"""
Evaluator implementation for Quarto CLI repository instances.

Builds a Docker image from the instance's Dockerfile, bootstraps the R
(``renv``), Python, and TinyTeX toolchains that Quarto's own test suite
depends on (mirroring ``tests/run-tests.sh``, which can't be invoked
directly since it assumes a Python virtualenv this testbed image doesn't
have), runs the suite via Deno's built-in test runner, and parses the
plain-text results.
"""

from __future__ import annotations

import logging
from typing import Final, override

from sbmdt.evaluator.base import Evaluator, TestResult
from sbmdt.evaluator.quarto.deno_test_parser import (
    results_text_to_test_results,
)

__all__ = [
    'QuartoEvaluator',
]

log = logging.getLogger(__name__)

TESTS_DIR: Final[str] = '/testbed/tests'
DENO_BIN: Final[str] = '/testbed/package/dist/bin/tools/deno'
QUARTO_SHARE_PATH: Final[str] = '/testbed/src/resources'
QUARTO_BIN_PATH: Final[str] = '/testbed/package/dist/bin'
IMPORT_MAP: Final[str] = '/testbed/src/import_map.json'
DENO_TEST_FLAGS: Final[str] = (
    '--unstable --allow-read --allow-write --allow-run --allow-env --allow-net'
)
# deno test runs files sequentially by default, so a single hung render (a
# LaTeX/xelatex compile that never returns has been observed in practice)
# would otherwise block the rest of the suite indefinitely. --kill-after
# escalates to SIGKILL if the process tree ignores the initial SIGTERM,
# which xelatex has been observed to do.
TEST_TIMEOUT_SECONDS: Final[int] = 1800
TIMEOUT_CMD: Final[str] = f'timeout --kill-after=30 {TEST_TIMEOUT_SECONDS}'


class QuartoEvaluator(Evaluator):
    """Evaluator for Quarto CLI benchmark instances.

    Bootstraps the R/Python/TinyTeX toolchain the test suite needs, then
    runs it via Deno's built-in test runner (``deno test``) and parses its
    plain-text summary output, since the Deno version bundled with these
    testbed images (1.22) predates structured (JUnit/JSON) reporter
    support.
    """

    @override
    def setup(self) -> None:
        """Bootstrap the R, Python, and TinyTeX toolchains tests depend on.

        Mirrors the first half of ``tests/run-tests.sh`` (everything before
        its ``deno test`` invocation), except the ``source bin/activate``
        step: these testbed images have no pre-built Python virtualenv, so
        packages install directly into the already-present system Python.

        Raises:
            Exception: If the container has not been started (i.e.,
                :meth:`Evaluator.provision` was not called first), or if
                the ``renv::restore()`` or TinyTeX install steps fail.
        """

        if self.container is None:
            raise Exception('no container')

        # 1. Ensure renv itself is present, then restore the R package
        # lockfile (rmarkdown, knitr, tinytex, etc.) it declares.
        exit_code, output = self.container.exec_run(
            [
                'Rscript',
                '-e',
                "if (!requireNamespace('renv', quietly = TRUE))"
                " install.packages('renv')",
            ],
            workdir=TESTS_DIR,
            stream=False,
        )
        assert isinstance(output, bytes)
        log.info(exit_code)
        log.info(output.decode())
        if exit_code != 0:
            raise Exception(
                f'Failed to install renv for {self.instance_id}: '
                f'{output.decode()}'
            )

        exit_code, output = self.container.exec_run(
            ['Rscript', '-e', 'renv::restore()'],
            workdir=TESTS_DIR,
            stream=False,
        )
        assert isinstance(output, bytes)
        log.info(exit_code)
        log.info(output.decode())
        if exit_code != 0:
            raise Exception(
                f'renv::restore() failed for {self.instance_id}: '
                f'{output.decode()}'
            )

        # 2. Install Python test dependencies. Not fatal on failure: this
        # old pip/Python combination reliably fails to build matplotlib
        # from source (a `canonicalize_version()` incompatibility), but
        # that package isn't needed by the Deno-based test runner itself.
        exit_code, output = self.container.exec_run(
            'pip3 install -r requirements.txt -q',
            workdir=TESTS_DIR,
            stream=False,
        )
        assert isinstance(output, bytes)
        log.info(exit_code)
        log.info(output.decode())
        if exit_code != 0:
            log.warning(
                f'pip install reported errors for {self.instance_id} '
                '(continuing; see log above)'
            )

        # 3. Install TinyTeX, needed by smoke tests that render to PDF.
        exit_code, output = self.container.exec_run(
            'quarto tools install tinytex',
            workdir=TESTS_DIR,
            stream=False,
        )
        assert isinstance(output, bytes)
        log.info(exit_code)
        log.info(output.decode())
        if exit_code != 0:
            raise Exception(
                f'Failed to install tinytex for {self.instance_id}: '
                f'{output.decode()}'
            )

        log.info('All changes applied successfully.')

    @override
    def evaluate(self) -> list[TestResult]:
        """Run the Deno test suite and parse its plain-text results.

        Returns:
            A list of :class:`TestResult` parsed from ``deno test``'s
            stdout/stderr.

        Raises:
            Exception: If the container has not been started (i.e.,
                ``setup`` was not called first).
        """

        if self.container is None:
            raise Exception('no container')

        command = (
            f'{TIMEOUT_CMD} {DENO_BIN} test {DENO_TEST_FLAGS}'
            f' --importmap={IMPORT_MAP}'
        )
        exit_code, output = self.container.exec_run(
            command,
            workdir=TESTS_DIR,
            environment={
                'NO_COLOR': '1',
                'QUARTO_BIN_PATH': QUARTO_BIN_PATH,
                'QUARTO_SHARE_PATH': QUARTO_SHARE_PATH,
                'QUARTO_DEBUG': 'true',
            },
            stream=False,
        )
        log.info('done running')
        assert isinstance(output, bytes)

        log.info(exit_code)
        decoded = output.decode()
        log.info(decoded)

        return results_text_to_test_results(
            self.instance_id,
            self.patch_type,
            self.agent_name,
            decoded,
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
