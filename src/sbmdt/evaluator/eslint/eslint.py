from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Final, override

from sbmdt.evaluator.base import Evaluator, TestResult
from sbmdt.evaluator.eslint.mocha_junit_parser import (
    results_xml_to_test_results,
)
from sbmdt.utils import read_from_container

__all__ = [
    'ESLintEvaluator',
]

log = logging.getLogger(__name__)

RESULTS_FILE: Final[str] = '/testbed/test-results.xml'

# Ordered list of test directories to pass to Mocha with --recursive.
# Mocha (2.x/3.x as used by older ESLint) expects directory paths when
# --recursive is set, not ** glob patterns.  Directories are checked for
# existence before being included.
_TEST_DIRS: Final[tuple[str, ...]] = (
    'tests/lib',
    'tests/bin',
    'tests/tools',
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

        Wrapping in a shell is necessary because ``exec_run`` does not
        invoke a shell on its own — it passes the command directly to the
        kernel, so operators like ``||`` and redirects like ``2>/dev/null``
        would be treated as literal arguments.

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
        assert exit_code is not None
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
        2. Run ``npx mocha --recursive`` against those directories, with
           ``mocha-junit-reporter`` writing JUnit XML to ``RESULTS_FILE``.
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
        # 'ls tests/' gives bare names (lib, bin, tools, ...) without shell
        # globbing so it works even if the tests/ directory is empty.
        # ------------------------------------------------------------------
        _, ls_output = self._exec('ls tests/ 2>/dev/null || true')
        existing_entries = set(ls_output.split())

        test_dirs: list[str] = []
        for dir_path in _TEST_DIRS:
            bare = dir_path.split('/')[-1]  # 'tests/lib' -> 'lib'
            if bare in existing_entries:
                test_dirs.append(dir_path)

        if not test_dirs:
            log.error('No test directories found under /testbed/tests/')
            return []

        log.info('Test directories: %s', test_dirs)

        # ------------------------------------------------------------------
        # Step 2: run Mocha with JUnit XML output.
        #
        # We pass directories + --recursive rather than ** glob patterns.
        # Older Mocha versions (2.x/3.x, used by ESLint 3–6) handle
        # --recursive correctly when given directory paths but may not
        # expand ** globs reliably.
        #
        # Flags:
        #   --recursive  – find all .js files inside each directory
        #   -t 10000     – 10 s per-test timeout (rule tests can be slow)
        #   --exit       – force-exit after suite finishes; some ESLint
        #                  tests leave async handles open
        # ------------------------------------------------------------------
        dirs_str = ' '.join(test_dirs)
        cmd = (
            f'npx mocha'
            f' --reporter mocha-junit-reporter'
            f' --reporter-options mochaFile={RESULTS_FILE}'
            f' --recursive'
            f' -t 10000'
            f' --exit'
            f' {dirs_str}'
        )

        log.info('Running: %s', cmd)
        exit_code, output = self._exec(cmd)
        log.info('Mocha exit code: %d', exit_code)
        log.info(output)

        # A non-zero exit is expected when tests fail — we still collect
        # the XML rather than aborting here.

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
                self.timestamp,
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
