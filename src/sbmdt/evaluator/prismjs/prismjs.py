"""
Evaluator implementation for PrismJS repository instances.

Builds a Docker image from the instance's Dockerfile, configures Mocha to
emit JUnit XML output via ``mocha-junit-reporter``, runs the two test
suites (test-runner self-tests, then the language grammar tests)
independently, and retrieves the combined results.
"""

from __future__ import annotations

import logging
from typing import Final, override

import docker.errors

from sbmdt.evaluator.base import Evaluator, TestResult
from sbmdt.evaluator.lighthouse.mocha_junit_parser import (
    results_xml_to_test_results,
)
from sbmdt.utils import read_from_container

__all__ = [
    'PrismjsEvaluator',
]

log = logging.getLogger(__name__)

RESULTS_DIR: Final[str] = '/testbed/test-results'
# PrismJS's own `npm test` script chains these two files with `&&`
# (`mocha tests/testrunner-tests.js && mocha tests/run.js`). They are run
# independently here (not `&&`-chained) so that one suite's failures don't
# prevent the other suite from running.
SUITES: Final[list[str]] = [
    'testrunner-tests',
    'run',
]


class PrismjsEvaluator(Evaluator):
    """Evaluator for PrismJS benchmark instances.

    Builds a Docker image for the given instance, installs and configures
    ``mocha-junit-reporter`` (pinned to its last 1.x release for
    compatibility with this project's old Mocha version), executes each
    suite's Mocha run independently, and reads the resulting XML files from
    the container.
    """

    @override
    def setup(self) -> None:
        """Install the JUnit reporter and create the results directory.

        Raises:
            Exception: If the container has not been started (i.e.,
                :meth:`Evaluator.provision` was not called first).
        """

        if self.container is None:
            raise Exception('no container')

        exit_code, output = self.container.exec_run(
            'npm install mocha-junit-reporter@1 --save-dev'
            ' --legacy-peer-deps',
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        if exit_code != 0:
            raise Exception(
                f'Failed to install mocha-junit-reporter for '
                f'{self.instance_id}: {output.decode()}'
            )

        self.container.exec_run(f'mkdir -p {RESULTS_DIR}', workdir='/testbed')

        log.info('All changes applied successfully.')

    @override
    def evaluate(self) -> list[TestResult]:
        """Run each Mocha suite and retrieve the combined JUnit XML results.

        Returns:
            A list of :class:`TestResult` parsed from both suites' JUnit
            XML output.

        Raises:
            Exception: If the container has not been started (i.e., ``setup``
                was not called first).
        """

        if self.container is None:
            raise Exception('no container')

        # Run each suite independently (joined with `;`, not `&&`), passing
        # the JUnit reporter directly on the command line since, unlike
        # Lighthouse, PrismJS invokes Mocha directly rather than through a
        # patchable shell script. `node_modules/.bin` is prepended to PATH
        # (rather than replacing it) so `mocha` resolves.
        commands = [
            'export PATH="/testbed/node_modules/.bin:$PATH"',
            *(
                f'mocha tests/{suite}.js --reporter mocha-junit-reporter'
                f' --reporter-options mochaFile={RESULTS_DIR}/{suite}.xml'
                for suite in SUITES
            ),
        ]
        exit_code, output = self.container.exec_run(
            ['bash', '-c', '; '.join(commands)],
            workdir='/testbed',
            stream=False,
        )
        log.info('done running')
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        results: list[TestResult] = []
        for suite in SUITES:
            try:
                xml = read_from_container(
                    self.container, f'{RESULTS_DIR}/{suite}.xml'
                )
            except docker.errors.NotFound:
                log.warning(f'No results file found for suite {suite}')
                continue

            results.extend(
                results_xml_to_test_results(
                    self.instance_id,
                    self.patch_type,
                    self.agent_name,
                    xml,
                )
            )

        return results

    @override
    def pre_cleanup(self) -> None:
        """Pre-cleanup hook. No-op for this evaluator."""
        pass

    @override
    def post_cleanup(self) -> None:
        """Post-cleanup hook. No-op for this evaluator."""
        pass
