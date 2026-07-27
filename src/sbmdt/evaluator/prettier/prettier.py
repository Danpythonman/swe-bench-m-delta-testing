"""
Evaluator implementation for Prettier repository instances.

Builds a Docker image from the instance's Dockerfile, runs the Jest test
suite with JSON output, and parses the results.

--- Findings from Dockerfile inspection ---

1. Test runner: Jest. The prettier/prettier repository has used Jest as its
   test runner across all versions covered by the SWE-bench instances.

2. Test invocation: Prettier's package.json exposes a ``jest`` script.
   The canonical command used by SWE-bench is:
       yarn jest --json --outputFile=/tmp/jest-results.json

3. Output format: Jest's built-in ``--json`` flag writes structured JSON
   to the path given by ``--outputFile``.

4. Working directory: ``/testbed`` (set in every prettier Dockerfile).

5. Environment variables: None required beyond what the base image sets.
"""

from __future__ import annotations

import logging
from typing import Final, override

from sbmdt.evaluator.base import Evaluator, TestResult
from sbmdt.evaluator.prettier.prettier_jest_parser import (
    results_json_to_test_results,
)
from sbmdt.utils import read_from_container

__all__ = [
    'PrettierEvaluator',
]

log = logging.getLogger(__name__)

JEST_OUTPUT_FILE: Final[str] = '/tmp/jest-results.json'

JEST_CMD: Final[list[str]] = [
    '/bin/sh',
    '-c',
    f'yarn jest --json --outputFile={JEST_OUTPUT_FILE}',
]


class PrettierEvaluator(Evaluator):
    """Evaluator for Prettier benchmark instances."""

    @override
    def setup(self) -> None:
        if self.container is None:
            raise Exception('no container')
        log.info('PrettierEvaluator setup: no extra steps needed.')

    @override
    def evaluate(self) -> list[TestResult]:
        if self.container is None:
            raise Exception('no container')

        exit_code, output = self.container.exec_run(
            JEST_CMD,
            workdir='/testbed',
            stream=False,
        )
        log.info('done running jest, exit code: %s', exit_code)
        assert isinstance(output, bytes)
        log.info(output.decode())

        results_json = read_from_container(self.container, JEST_OUTPUT_FILE)

        return results_json_to_test_results(
            self.instance_id,
            self.patch_type,
            self.agent_name,
            results_json,
            self.timestamp,
        )

    @override
    def pre_cleanup(self) -> None:
        pass

    @override
    def post_cleanup(self) -> None:
        pass
