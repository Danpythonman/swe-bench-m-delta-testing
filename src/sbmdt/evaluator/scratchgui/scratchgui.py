"""
Evaluator implementation for scratch-gui (scratchfoundation/scratch-gui)
repository instances.

Builds a Docker image from the instance's Dockerfile, runs the Jest unit
test suite with JSON output, and parses the results.

--- Findings from Dockerfile / package.json inspection ---

1. Test runner: Jest. scratch-gui's ``package.json`` exposes:
       "test": "npm run test:lint && npm run test:unit && npm run build &&
                npm run test:integration"
       "test:unit": "jest test[\\/]unit"
       "test:integration": "jest --maxWorkers=4 test[\\/]integration"
       "test:lint": "eslint . --ext .js,.jsx"

   The chained ``npm test`` command is unsuitable for automated evaluation
   for two reasons:
     - ``test:lint`` is joined with ``&&``, so any lint error (which is
       unrelated to functional correctness, and scratch-gui's own
       ``develop`` branch has pre-existing lint errors) aborts the whole
       chain before ``test:unit`` ever runs.
     - ``test:integration`` requires a full webpack ``build`` and a
       Selenium/headless-browser environment, which is not available (or
       desirable) in this container-based evaluation harness.

   This evaluator therefore invokes Jest directly against the unit test
   directory only, bypassing lint, build, and integration tests entirely.

2. Test invocation: the canonical command used by this evaluator is:
       npx jest test/unit --json --outputFile=/tmp/jest-results.json

   Passing ``test/unit`` (rather than reproducing the ``test[\\/]unit``
   regex from package.json) is safe because the container is always Linux,
   so the path separator is always ``/``; Jest treats the positional
   argument as a regex, and ``/`` has no special regex meaning.

3. Output format: Jest's built-in ``--json`` flag writes structured JSON
   to the path given by ``--outputFile``. This works across the wide range
   of Jest versions used by different scratch-gui instances (this project's
   history spans Jest ~21 up to modern Jest) without requiring any extra
   reporter package to be installed, unlike jest-junit-based approaches.

4. Working directory: ``/testbed`` (set in every scratch-gui Dockerfile,
   consistent with the base SWE-bench harness images).

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
    'ScratchGuiEvaluator',
]

log = logging.getLogger(__name__)

JEST_OUTPUT_FILE: Final[str] = '/tmp/jest-results.json'

# Only the unit suite is run; see the module docstring for why the chained
# `npm test` (lint && unit && build && integration) is not used.
JEST_CMD: Final[list[str]] = [
    'npx', 'jest', 'test/unit',
    '--json', f'--outputFile={JEST_OUTPUT_FILE}',
]


class ScratchGuiEvaluator(Evaluator):
    """Evaluator for scratch-gui benchmark instances.

    Builds a Docker image for the given instance, runs Jest directly
    against ``test/unit`` with the built-in ``--json`` reporter, and reads
    the resulting JSON from the container. No extra setup (package
    installs or config patching) is required since ``--json`` is a
    built-in Jest flag supported across every Jest version this project
    has used.
    """

    @override
    def setup(self) -> None:
        if self.container is None:
            raise Exception('no container')
        log.info('ScratchGuiEvaluator setup: no extra steps needed.')

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
        )

    @override
    def pre_cleanup(self) -> None:
        pass

    @override
    def post_cleanup(self) -> None:
        pass
