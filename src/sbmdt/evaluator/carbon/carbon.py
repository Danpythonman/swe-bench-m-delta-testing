"""
Evaluator implementation for Carbon Design System repository instances.

Builds a Docker image from the instance's Dockerfile, runs the Jest test
suite (which already has ``jest-junit`` configured as a reporter in
``jest.config.js``), and retrieves the results.
"""

from __future__ import annotations

import logging
from typing import Final, override

from sbmdt.evaluator.base import Evaluator, TestResult
from sbmdt.evaluator.grommet.jest_junit_parser import (
    results_xml_to_test_results,
)
from sbmdt.utils import read_from_container

__all__ = [
    'CarbonEvaluator',
]

log = logging.getLogger(__name__)

RESULTS_DIR: Final[str] = 'test-results'
RESULTS_FILE: Final[str] = 'results.xml'


class CarbonEvaluator(Evaluator):
    """Evaluator for Carbon Design System benchmark instances.

    Carbon's ``jest.config.js`` already declares ``jest-junit`` in Jest's
    ``reporters`` array, so no patching is needed. This evaluator only
    ensures the output directory exists, then runs ``npm test`` with the
    ``JEST_JUNIT_OUTPUT_DIR``/``JEST_JUNIT_OUTPUT_NAME`` environment
    variables pointing to the desired results file.
    """

    @override
    def setup(self) -> None:
        """Create the directory where jest-junit will write its XML output.

        Raises:
            Exception: If the container has not been started (i.e.,
                :meth:`Evaluator.provision` was not called first).
        """

        if self.container is None:
            raise Exception('no container')

        exit_code, output = self.container.exec_run(
            f'mkdir -p /testbed/{RESULTS_DIR}',
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        if exit_code != 0:
            raise Exception(
                f'Failed to create test-results directory for '
                f'{self.instance_id}: {output.decode()}'
            )

    @override
    def evaluate(self) -> list[TestResult]:
        """Run ``npm test`` and retrieve the JUnit XML results.

        Uses the ``JEST_JUNIT_OUTPUT_DIR``/``JEST_JUNIT_OUTPUT_NAME``
        environment variables to direct output to a known location.
        jest-junit v10 (the version installed here) does not support the
        older single-path ``JEST_JUNIT_OUTPUT`` variable.

        Returns:
            A list of :class:`TestResult` parsed from the JUnit XML output.

        Raises:
            Exception: If the container has not been started.
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
        log.info('done running npm test')
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
        )

    @override
    def pre_cleanup(self) -> None:
        """Pre-cleanup hook. No-op for this evaluator."""
        pass

    @override
    def post_cleanup(self) -> None:
        """Post-cleanup hook. No-op for this evaluator."""
        pass
