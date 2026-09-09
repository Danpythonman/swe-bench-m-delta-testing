"""
Evaluator implementation for Alibaba repository instances.

Builds a Docker image from the instance's Dockerfile, configures the Karma
test runner to emit JUnit XML output, runs the test suite, and retrieves
the results.
"""

from __future__ import annotations

import logging
from typing import Final, override

from sbmdt.evaluator.alibaba.karma_junit_parser import (
    results_xml_to_test_results,
)
from sbmdt.evaluator.base import Evaluator, TestResult
from sbmdt.utils import (
    apply_change_literal,
    apply_change_regex,
    read_from_container,
)

__all__ = [
    'AlibabaEvaluator',
]

log = logging.getLogger(__name__)

KARMA_FILE: Final[str] = '/testbed/scripts/test/karma.js'
PATCH_FILE: Final[str] = '/tmp/model.patch'


class AlibabaEvaluator(Evaluator):
    """Evaluator for Alibaba benchmark instances.

    Builds a Docker image for the given instance, patches the Karma
    configuration to produce JUnit XML output, executes ``npm test``,
    and reads the resulting XML from the container.
    """

    @override
    def setup(self) -> None:
        """Install the JUnit reporter and patch Karma's config.

        Steps performed:
        1. Install ``karma-junit-reporter`` in the container.
        2. Patch ``karma.js`` to add the JUnit reporter, its config block,
           and the plugin entry.

        Raises:
            Exception: If the container has not been started (i.e.,
                :meth:`Evaluator.provision` was not called first).
        """

        if self.container is None:
            raise Exception('no container')

        # 1. Install package
        #
        # next-2984, next-3454 and next-4182 all failed here with
        # "npm: executable file not found in $PATH" even though npm
        # plainly exists in this environment (other instances install it
        # successfully) -- exec_run's default invocation does not source
        # a login shell, so if node/npm were installed via nvm (which adds
        # itself to PATH from ~/.bashrc / ~/.profile, not the image's
        # baked-in ENV PATH), a bare exec never sees it. Running through
        # `bash -lc` sources that profile the way an interactive shell
        # would.
        exit_code, output = self.container.exec_run(
            ['bash', '-lc', 'npm install karma-junit-reporter --save-dev'],
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        # next-2984 hit "npm: executable file not found in $PATH" here
        # (exit_code 126) and setup() logged "All changes applied
        # successfully" anyway, since nothing checked this step's exit
        # code the way every other step below (and every other
        # evaluator) does. The steps that follow only edit text files and
        # so happened not to depend on this one succeeding, but silently
        # continuing past a failed dependency install is not something to
        # rely on staying harmless.
        if exit_code != 0:
            raise Exception(
                f'Failed to install karma-junit-reporter for '
                f'{self.instance_id}: {output.decode()}'
            )

        # 2. Add junit to reporters
        apply_change_literal(
            container=self.container,
            file=KARMA_FILE,
            find="reporters: ['spec', 'coverage']",
            replace="reporters: ['spec', 'coverage', 'junit']",
            assertion="reporters: ['spec', 'coverage', 'junit']",
        )

        # 3. Add junitReporter config
        apply_change_literal(
            container=self.container,
            file=KARMA_FILE,
            find="hostname: 'localhost'",
            replace="""junitReporter: {
                    outputDir: 'test-results',
                    outputFile: 'results.xml',
                    useBrowserName: false,
                },
                hostname: 'localhost'""",
            assertion='junitReporter:',
        )

        # 4. Add plugin
        apply_change_regex(
            container=self.container,
            file=KARMA_FILE,
            find=r"'karma-coverage',?",
            replace=lambda m: (
                "'karma-coverage',\n            'karma-junit-reporter',"
            ),
            assertion="'karma-junit-reporter',",
        )

        log.info('All changes applied successfully.')

    @override
    def evaluate(self) -> list[TestResult]:
        """Run ``npm test`` and retrieve the JUnit XML results.

        Returns:
            A list of :class:`TestResult` parsed from the JUnit XML output.

        Raises:
            Exception: If the container has not been started (i.e., ``setup``
                was not called first).
        """

        if self.container is None:
            raise Exception('no container')

        exit_code, output = self.container.exec_run(
            ['bash', '-lc', 'npm test'],
            environment={'TRAVIS': 'true'},
            workdir='/testbed',
            stream=False,
        )
        log.info('done running')
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        # junitReporter's outputDir is relative to karma's basePath, which
        # is not guaranteed to put results.xml next to karma.js -- two
        # instances (next-4182, next-3454) failed with the results file
        # missing at exactly that assumed path. Finding whatever karma
        # actually wrote, the same way openlayers' evaluator does, instead
        # of assuming a fixed location.
        default_results_file = (
            '/testbed/scripts/test/test-results/results.xml'
        )
        _, results_find = self.container.exec_run(
            [
                'find',
                '/testbed',
                '-name',
                'results.xml',
                '-not',
                '-path',
                '*/node_modules/*',
            ],
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(results_find, bytes)
        written = [
            p for p in results_find.decode().splitlines() if p.strip()
        ]
        if default_results_file in written:
            results_file = default_results_file
        elif written:
            results_file = written[0]
            log.info(
                f'results.xml for {self.instance_id} is at '
                f'{results_file!r}, not the expected '
                f'{default_results_file!r}'
            )
        else:
            raise Exception(
                f'karma wrote no results.xml for {self.instance_id}; '
                f'expected it at {default_results_file!r}'
            )

        results = read_from_container(self.container, results_file)

        return results_xml_to_test_results(
            self.instance_id,
            self.patch_type,
            self.agent_name,
            results,
            self.timestamp,
        )

    @override
    def pre_cleanup(self) -> None:
        """Pre-cleanup hook. No-op for this evaluator."""
        pass

    @override
    def post_cleanup(self) -> None:
        """Post-cleanup hook. No-op for this evaluator."""
        pass
