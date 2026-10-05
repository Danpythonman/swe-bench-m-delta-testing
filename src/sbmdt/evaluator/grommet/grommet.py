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

        Some grommet versions define ``test`` as ``jest --runInBand && yarn
        test-timezones``, which re-runs the Calendar and DateInput suites
        under three time zones. With a single output name each of those
        runs overwrote the main run's XML, so a passing main run (the gold
        patch) kept only the last 81 timezone tests. Every Jest run now
        writes its own file, and the files are folded together: a test
        fails if it failed in any run.

        Returns:
            A list of :class:`TestResult` parsed from the JUnit XML output.

        Raises:
            Exception: If the container has not been started (i.e., ``setup``
                was not called first), or if Jest wrote no XML.
        """

        if self.container is None:
            raise Exception('no container')

        self.container.exec_run(
            f'rm -rf {RESULTS_DIR}', workdir='/testbed', stream=False
        )
        exit_code, output = self.container.exec_run(
            'npm test',
            environment={
                'JEST_JUNIT_OUTPUT_DIR': RESULTS_DIR,
                'JEST_JUNIT_UNIQUE_OUTPUT_NAME': 'true',
            },
            workdir='/testbed',
            stream=False,
        )
        log.info('done running')
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        _, listing = self.container.exec_run(
            f'find {RESULTS_DIR} -name "*.xml"',
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(listing, bytes)
        xml_files = sorted(listing.decode().split())
        if not xml_files:
            raise Exception(f'jest-junit wrote no XML for {self.instance_id}')
        log.info(f'jest-junit wrote {len(xml_files)} file(s): {xml_files}')

        merged: dict[str, TestResult] = {}
        for xml_file in xml_files:
            results = results_xml_to_test_results(
                self.instance_id,
                self.patch_type,
                self.agent_name,
                read_from_container(self.container, f'/testbed/{xml_file}'),
                self.timestamp,
            )
            for result in results:
                previous = merged.get(result.test_name)
                if previous is None or previous.passed:
                    merged[result.test_name] = result
        return list(merged.values())

    @override
    def pre_cleanup(self) -> None:
        """Pre-cleanup hook. No-op for this evaluator."""
        pass

    @override
    def post_cleanup(self) -> None:
        """Post-cleanup hook. No-op for this evaluator."""
        pass
