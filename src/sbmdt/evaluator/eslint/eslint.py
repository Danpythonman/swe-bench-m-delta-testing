"""
Evaluator implementation for ESLint repository instances.

Installs ``mocha-junit-reporter`` in the container, runs the ESLint Mocha
test suite directly (bypassing ``Makefile.js`` to avoid the Karma/PhantomJS
browser runner), and parses the resulting JUnit XML.

Why bypass ``Makefile.js``?
---------------------------
``node Makefile.js test`` runs two distinct phases: a Mocha server-side
phase (``tests/lib``, ``tests/bin``, ``tests/tools``) and a Karma/PhantomJS
browser phase.  The browser phase is irrelevant for delta-testing and would
require PhantomJS to be present and functional.  Running Mocha directly
gives us clean, structured JUnit XML without browser dependencies.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Final, override

from sbmdt.evaluator.base import Evaluator, TestResult
from sbmdt.evaluator.eslint.mocha_junit_parser import results_xml_to_test_results
from sbmdt.utils import read_from_container

__all__ = [
    'ESLintEvaluator',
]

log = logging.getLogger(__name__)

RESULTS_FILE: Final[str] = '/testbed/test-results.xml'

# Ordered list of (directory-to-check, mocha-glob) pairs.
# Only globs whose directory exists in the container are passed to Mocha.
_TEST_GLOBS: Final[tuple[tuple[str, str], ...]] = (
    ('tests/lib',   'tests/lib/**/*.js'),
    ('tests/bin',   'tests/bin/**/*.js'),
    ('tests/tools', 'tests/tools/**/*.js'),
)


class ESLintEvaluator(Evaluator):
    """Evaluator for ESLint benchmark instances.

    Builds a Docker image for the given instance, installs
    ``mocha-junit-reporter`` so that Mocha emits JUnit XML, discovers
    which test directories are present, runs Mocha directly (not via
    ``Makefile.js``), and parses the resulting XML.
    """

    def _exec(self, cmd: str) -> tuple[int, str]:
        """Run ``cmd`` via ``/bin/bash -c`` in ``/testbed``.

        Wrapping in a shell is required because several commands use shell
        features: glob patterns for ``ls``, ``||`` for fallbacks, and
        ``**`` glob expansion that Mocha expects the shell to leave alone
        (Mocha's own glob library handles ``**``, but the argument must
        arrive without surrounding quote characters).

        Args:
            cmd: Shell command to execute inside the container.

        Returns:
            A tuple of the numeric exit code and decoded stdout/stderr.

        Raises:
            RuntimeError: If the container has not been started.
        """
        if self.container is None:
            raise RuntimeError('Container not initialized')

        exit_code, output = self.container.exec_run(
            ['/bin/bash', '-c', cmd],
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)
        return exit_code, output.decode('utf-8', errors='replace')

    @override
    def setup(self) -> None:
        """Install ``mocha-junit-reporter`` in the container.

        Uses ``--no-save`` so that ``package.json`` is not modified, which
        keeps the working tree clean for ``git apply`` later.

        Raises:
            RuntimeError: If the container has not been started.
        """
        if self.container is None:
            raise RuntimeError('Container not initialized')

        log.info('Installing mocha-junit-reporter...')
        exit_code, output = self._exec(
            'npm install --no-save mocha-junit-reporter@1.18.0'
        )
        log.info('npm install exit code: %d', exit_code)
        log.info(output)
        log.info('Setup complete')

    @override
    def evaluate(self) -> list[TestResult]:
        """Run the Mocha test suite and return parsed results.

        Steps:
        1. Detect which of ``tests/lib``, ``tests/bin``, ``tests/tools``
           exist in the container.
        2. Run ``npx mocha`` with ``mocha-junit-reporter`` writing to
           ``RESULTS_FILE``.
        3. Read and parse the JUnit XML via
           :func:`~sbmdt.evaluator.eslint.mocha_junit_parser.results_xml_to_test_results`.

        Returns:
            A list of :class:`~sbmdt.evaluator.base.TestResult`, one per
            non-skipped ``<testcase>`` element in the XML.  Returns an empty
            list if no test directories are found or the XML cannot be read
            or parsed.

        Raises:
            RuntimeError: If the container has not been started.
        """
        if self.container is None:
            raise RuntimeError('Container not initialized')

        # ------------------------------------------------------------------
        # Step 1: discover which test directories exist in this ESLint version.
        # Uses 'ls tests/' (no glob) so the shell is not required for this
        # specific check, but _exec still wraps in bash for consistency.
        # ------------------------------------------------------------------
        _, ls_output = self._exec('ls tests/ 2>/dev/null || true')
        existing_entries = set(ls_output.split())

        test_globs: list[str] = []
        for dir_name, glob in _TEST_GLOBS:
            # ls output gives bare names like 'lib', 'bin', 'tools'
            # dir_name is 'tests/lib' etc., so check the basename portion
            bare = dir_name.split('/')[-1]
            if bare in existing_entries:
                test_globs.append(glob)

        if not test_globs:
            log.error('No test directories found under /testbed/tests/')
            return []

        log.info('Test globs: %s', test_globs)

        # ------------------------------------------------------------------
        # Step 2: run Mocha with JUnit XML output.
        #
        # Flags:
        #   --recursive  – descend into subdirectories (not all mocha versions
        #                  support ** globs without this)
        #   -t 10000     – 10 s per-test timeout (rule tests can be slow)
        #   --exit       – force-exit after suite finishes; some ESLint tests
        #                  leave async handles open
        #
        # The globs are shell-quoted so the shell passes them verbatim to
        # Mocha's own glob resolver (which handles ** internally).
        # ------------------------------------------------------------------
        globs_str = ' '.join(f"'{g}'" for g in test_globs)
        cmd = (
            f'npx mocha'
            f' --reporter mocha-junit-reporter'
            f' --reporter-options mochaFile={RESULTS_FILE}'
            f' --recursive'
            f' -t 10000'
            f' --exit'
            f' {globs_str}'
        )

        log.info('Running: %s', cmd)
        exit_code, output = self._exec(cmd)
        log.info('Mocha exit code: %d', exit_code)
        log.info(output)

        # A non-zero exit is expected when tests fail — we still collect the
        # XML output rather than aborting here.

        # ------------------------------------------------------------------
        # Step 3: read XML from container
        # ------------------------------------------------------------------
        try:
            xml_content = read_from_container(self.container, RESULTS_FILE)
        except Exception as exc:
            log.error(
                'Could not read results XML (tests may have crashed): %s', exc
            )
            return []

        # ------------------------------------------------------------------
        # Step 4: parse XML via shared parser
        # ------------------------------------------------------------------
        try:
            return results_xml_to_test_results(
                self.instance_id,
                self.patch_type,
                self.agent_name,
                xml_content,
            )
        except ET.ParseError as exc:
            log.error('JUnit XML parse error: %s', exc)
            return []

    @override
    def pre_cleanup(self) -> None:
        """Pre-cleanup hook. No-op for this evaluator."""
        pass

    @override
    def post_cleanup(self) -> None:
        """Post-cleanup hook. No-op for this evaluator."""
        pass