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
   For newer ESM-based versions of prettier (>=3.0), Node must be invoked
   with ``--experimental-vm-modules``:
       node --experimental-vm-modules node_modules/.bin/jest \
           --json --outputFile=/tmp/jest-results.json
   # TODO: confirm which command applies to each instance by inspecting the
   # base image's package.json at /testbed/package.json and checking the
   # "type" field or the Node version.

3. Output format: Jest's built-in ``--json`` flag writes structured JSON
   to the path given by ``--outputFile``. No extra reporter package is
   needed (unlike Karma in the Alibaba evaluator). The file is retrieved
   via :func:`~sbmdt.utils.read_from_container` and parsed by
   :mod:`prettier_jest_parser`.

4. Working directory: ``/testbed`` (set in every prettier Dockerfile).

5. Environment variables: None required beyond what the base image sets.
   The Alibaba evaluator sets ``TRAVIS=true`` to suppress browser launches;
   prettier does not need this.
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

# TODO: prettier >=3.0 requires --experimental-vm-modules. Determine
# per-instance whether to use the yarn shorthand or the explicit node
# invocation by reading /testbed/package.json from the container in
# setup() and checking "engines.node" or the package "type" field.
JEST_CMD: Final[str] = (
    f'yarn jest --json --outputFile={JEST_OUTPUT_FILE}'
)


class PrettierEvaluator(Evaluator):
    """Evaluator for Prettier benchmark instances.

    Builds a Docker image for the given instance, runs the Jest test suite
    with JSON output enabled, reads the result file from the container, and
    parses it into :class:`~sbmdt.evaluator.base.TestResult` objects.
    """

    @override
    def setup(self) -> None:
        """No extra setup required for Prettier instances.

        The base Docker image already contains all dependencies. Jest's
        ``--json`` flag is built-in, so no additional reporter package needs
        to be installed.

        Raises:
            Exception: If the container has not been started (i.e.,
                :meth:`Evaluator.provision` was not called first).
        """

        if self.container is None:
            raise Exception('no container')

        log.info('PrettierEvaluator setup: no extra steps needed.')

    @override
    def evaluate(self) -> list[TestResult]:
        """Run Jest and retrieve the JSON results.

        Executes ``yarn jest --json --outputFile=<path>`` inside the
        container, reads the resulting JSON file, and parses it into
        :class:`~sbmdt.evaluator.base.TestResult` objects.

        Returns:
            A list of :class:`~sbmdt.evaluator.base.TestResult` parsed from
            Jest's JSON output.

        Raises:
            Exception: If the container has not been started (i.e.,
                :meth:`setup` was not called first).
        """

        if self.container is None:
            raise Exception('no container')

        exit_code, output = self.container.exec_run(
            JEST_CMD,
            workdir='/testbed',
            stream=False,
        )
        log.info('done running jest')
        assert isinstance(output, bytes)

        log.info(exit_code)
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
        """Pre-cleanup hook. No-op for this evaluator."""
        pass

    @override
    def post_cleanup(self) -> None:
        """Post-cleanup hook. No-op for this evaluator."""
        pass