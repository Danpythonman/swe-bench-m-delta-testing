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
       jest test/unit --runInBand --json --outputFile=/tmp/jest-results.json

   run via the locally installed binary (``node_modules/.bin/jest``,
   prepended to ``PATH``), the same way :class:`LighthouseEvaluator` invokes
   Mocha. ``npx jest ...`` was tried first but is unreliable here: if npx's
   local-binary resolution fails for any reason (npm/npx version quirks
   across scratch-gui's long instance history, no ``node_modules/.bin/jest``
   on ``PATH`` in some setups, etc.) it can silently fall back to attempting
   a network fetch or an interactive confirmation prompt with no TTY to
   answer it, resulting in Jest never running and no output file ever being
   written, with no clear error. Invoking the binary directly sidesteps
   that resolution step entirely.

   ``--runInBand`` (single in-process run, no worker-farm) is used as a
   defensive measure: at least one instance
   (``test/unit/util/vm-manager-hoc.test.jsx``, which exercises
   ``AudioContext``/jsdom) can crash a worker child process outright, and
   running in-band avoids the extra process-isolation layer that crash was
   observed through.

   The actual root cause of that crash, however, is a Node.js version
   mismatch, not worker-farm itself: scratch-gui's test suite was written
   against Node <=14, where an unhandled promise rejection only printed a
   warning. Since Node 15, the default behavior changed to terminate the
   process on an unhandled rejection (see
   https://nodejs.org/api/cli.html#--unhandled-rejectionsmode), and the
   containers this evaluator runs in use a modern Node (v20+). One of
   scratch-gui's own dependencies (``startaudiocontext``, exercised via
   ``vm-manager-hoc.test.jsx``) has a genuinely unhandled rejection that
   was always latent but harmless under old Node, and is now fatal: it
   kills the Node process outright (confirmed by the process exiting with
   a bare ``Node.js vX.Y.Z`` line and no further Jest output at all -- not
   a normal, reporter-visible test failure). This is fixed by setting
   ``NODE_OPTIONS=--unhandled-rejections=warn`` for the jest invocation,
   which restores the old "print a warning and keep going" behavior;
   ``--unhandled-rejections`` is one of the flags Node explicitly allows to
   be set via ``NODE_OPTIONS``, so this applies regardless of how Jest's
   own binary happens to be invoked (its shebang script, workers spawned
   by worker-farm, etc.), rather than needing to inject a raw ``node``
   invocation.

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

# See point 2 in the module docstring: restores Node <=14's "warn, don't
# crash" behavior for unhandled promise rejections, which scratch-gui's
# test suite implicitly relies on but the container's modern Node no
# longer does by default.
JEST_ENV: Final[dict[str, str]] = {
    'NODE_OPTIONS': '--unhandled-rejections=warn',
}

# Only the unit suite is run; see the module docstring for why the chained
# `npm test` (lint && unit && build && integration) is not used.
#
# Invoked via `bash -c` with node_modules/.bin prepended to PATH (rather
# than `npx jest ...`) so resolution always hits the locally installed
# binary directly; see point 2 in the module docstring for why npx is
# avoided. PATH is prepended to, not replaced, since node itself may live
# somewhere non-standard in a given instance's image.
JEST_SHELL_CMD: Final[str] = (
    'export PATH="/testbed/node_modules/.bin:$PATH" && '
    f'jest test/unit --runInBand --json --outputFile={JEST_OUTPUT_FILE}'
)
JEST_CMD: Final[list[str]] = ['bash', '-c', JEST_SHELL_CMD]


class ScratchGuiEvaluator(Evaluator):
    """Evaluator for scratch-gui benchmark instances.

    Builds a Docker image for the given instance, runs Jest directly
    against ``test/unit`` with the built-in ``--json`` reporter, and reads
    the resulting JSON from the container. No extra setup (package
    installs or config patching) is required since ``--json`` is a
    built-in Jest flag supported across every Jest version this project
    has used. The run is set to ``--runInBand`` and given
    ``NODE_OPTIONS=--unhandled-rejections=warn``; see the module docstring
    for why both are needed to get a reliable result out of this test
    suite under a modern Node runtime.
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
            environment=JEST_ENV,
            workdir='/testbed',
            stream=False,
        )
        log.info('done running jest, exit code: %s', exit_code)
        assert isinstance(output, bytes)
        decoded_output = output.decode()
        log.info(decoded_output)

        # Jest exits non-zero whenever any test fails, which is the normal,
        # expected case here, so exit_code alone can't signal a problem.
        # What does signal a problem is the output file never having been
        # written at all (e.g. jest/node not found, a crash before any
        # reporter ran, wrong working directory for this instance, etc.).
        # Checking for that explicitly here, and raising with the captured
        # jest output attached, turns what would otherwise be an opaque
        # `docker.errors.NotFound` at the read step below into a message
        # that actually explains what went wrong.
        check_exit_code, _ = self.container.exec_run(
            ['test', '-f', JEST_OUTPUT_FILE],
            workdir='/testbed',
            stream=False,
        )
        if check_exit_code != 0:
            raise Exception(
                f'{self.instance_id}: jest did not produce '
                f'{JEST_OUTPUT_FILE} (exit code {exit_code}). This usually '
                'means the jest command itself failed to run (rather than '
                'running and reporting test failures). Captured output:\n'
                f'{decoded_output}'
            )

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
