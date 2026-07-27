"""
Evaluator implementation for highlight.js repository instances.

Builds a Docker image from the instance's Dockerfile, installs
``mocha-junit-reporter``, runs the Mocha test suite, and retrieves the
JUnit XML results.

--- Findings from Dockerfile inspection ---

1. Test runner: Mocha. The highlightjs/highlight.js repository uses Mocha
   as its test runner across all versions covered by the SWE-bench
   instances.

2. Test invocation: The ``npm test`` script delegates to Mocha. The JUnit
   reporter is injected via the npm pass-through separator::

       npm test -- --reporter mocha-junit-reporter

   The output file path is controlled by the ``MOCHA_FILE`` environment
   variable consumed by ``mocha-junit-reporter``.

   # TODO: confirm that the npm test script in each base image passes
   # extra arguments through to Mocha (i.e. is ``mocha [opts]`` rather
   # than a custom Node script). If not, invoke Mocha directly:
   #   node_modules/.bin/mocha --reporter mocha-junit-reporter [opts]

3. Output format: ``mocha-junit-reporter`` writes standard JUnit XML to
   the path in ``MOCHA_FILE``. The ``<testcase>`` elements are nested
   inside ``<testsuite>`` elements (unlike Karma's flat output), so the
   parser uses a recursive XPath search (``'.//testcase'``).

4. Working directory: ``/testbed`` (set in every highlightjs Dockerfile).

5. Environment variables: ``MOCHA_FILE`` must be set to the desired output
   path for ``mocha-junit-reporter``. No other environment variables are
   required beyond what the base image provides.
"""

from __future__ import annotations

import logging
from typing import Final, override

from sbmdt.evaluator.base import Evaluator, TestResult
from sbmdt.evaluator.highlightjs.highlightjs_mocha_junit_parser import (
    results_xml_to_test_results,
)
from sbmdt.utils import read_from_container

__all__ = [
    'HighlightjsEvaluator',
]

log = logging.getLogger(__name__)

MOCHA_OUTPUT_FILE: Final[str] = '/tmp/test-results.xml'
TEST_CMD: Final[str] = (
    'npm test -- --reporter mocha-junit-reporter'
)


class HighlightjsEvaluator(Evaluator):
    """Evaluator for highlight.js benchmark instances.

    Builds a Docker image for the given instance, installs
    ``mocha-junit-reporter``, executes ``npm test`` with the JUnit
    reporter enabled, reads the resulting XML from the container, and
    parses it into :class:`~sbmdt.evaluator.base.TestResult` objects.
    """

    @override
    def setup(self) -> None:
        """Install ``mocha-junit-reporter`` in the container.

        Raises:
            Exception: If the container has not been started (i.e.,
                :meth:`Evaluator.provision` was not called first).
        """

        if self.container is None:
            raise Exception('no container')

        exit_code, output = self.container.exec_run(
            'npm install mocha-junit-reporter --save-dev',
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

    @override
    def evaluate(self) -> list[TestResult]:
        """Run ``npm test`` with the JUnit reporter and retrieve results.

        Executes the test suite with ``mocha-junit-reporter`` writing
        output to ``MOCHA_FILE``, then reads and parses that file.

        Returns:
            A list of :class:`~sbmdt.evaluator.base.TestResult` parsed
            from the JUnit XML output.

        Raises:
            Exception: If the container has not been started (i.e.,
                :meth:`setup` was not called first).
        """

        if self.container is None:
            raise Exception('no container')

        exit_code, output = self.container.exec_run(
            TEST_CMD,
            environment={'MOCHA_FILE': MOCHA_OUTPUT_FILE},
            workdir='/testbed',
            stream=False,
        )
        log.info('done running mocha')
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        results_xml = read_from_container(
            self.container, MOCHA_OUTPUT_FILE
        )

        return results_xml_to_test_results(
            self.instance_id,
            self.patch_type,
            self.agent_name,
            results_xml,
        )

    @override
    def pre_cleanup(self) -> None:
        """Pre-cleanup hook. No-op for this evaluator."""
        pass

    @override
    def post_cleanup(self) -> None:
        """Post-cleanup hook. No-op for this evaluator."""
        pass