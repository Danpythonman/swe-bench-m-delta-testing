"""
Evaluator implementation for Grommet repository instances.

Builds a Docker image from the instance's Dockerfile, configures Jest to
emit JUnit XML output via ``jest-junit``, runs the test suite, and
retrieves the results.
"""

from __future__ import annotations

import logging
from typing import Final, override

from sbmdt.evaluator.base import Evaluator, TestResult
from sbmdt.evaluator.grommet.jest_junit_parser import (
    results_xml_to_test_results,
)
from sbmdt.utils import apply_change_regex, read_from_container

__all__ = [
    'GrommetEvaluator',
]

log = logging.getLogger(__name__)

PACKAGE_JSON_FILE: Final[str] = '/testbed/package.json'
RESULTS_DIR: Final[str] = 'test-results'
RESULTS_FILE: Final[str] = 'results.xml'


class GrommetEvaluator(Evaluator):
    """Evaluator for Grommet benchmark instances.

    Builds a Docker image for the given instance, installs and configures
    ``jest-junit`` to produce JUnit XML output, executes ``npm test``, and
    reads the resulting XML from the container.
    """

    @override
    def setup(self) -> None:
        """Install the JUnit reporter and enable it in Jest's config.

        Steps performed:
        1. Install ``jest-junit`` in the container.
        2. Patch ``package.json`` to add ``jest-junit`` to Jest's
           ``reporters`` list, alongside the default reporter.

        Raises:
            Exception: If the container has not been started (i.e.,
                :meth:`Evaluator.provision` was not called first).
        """

        if self.container is None:
            raise Exception('no container')

        # 1. Install package
        exit_code, output = self.container.exec_run(
            'npm install jest-junit --save-dev --legacy-peer-deps',
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        if exit_code != 0:
            raise Exception(
                f'Failed to install jest-junit for {self.instance_id}: '
                f'{output.decode()}'
            )

        # 2. Add jest-junit to reporters, anchoring only on the opening of
        # Jest's config block since the fields that follow it vary between
        # instances.
        apply_change_regex(
            container=self.container,
            file=PACKAGE_JSON_FILE,
            find=r'"jest":\s*\{',
            replace=(
                '"jest": {\n'
                '    "reporters": [\n'
                '      "default",\n'
                '      "jest-junit"\n'
                '    ],'
            ),
            assertion='"reporters": [\n      "default",\n      "jest-junit"',
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
            'npm test',
            environment={
                'JEST_JUNIT_OUTPUT_DIR': RESULTS_DIR,
                'JEST_JUNIT_OUTPUT_NAME': RESULTS_FILE,
            },
            workdir='/testbed',
            stream=False,
        )
        log.info('done running')
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())
        results = read_from_container(
            self.container, f'/testbed/{RESULTS_DIR}/{RESULTS_FILE}'
        )

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
