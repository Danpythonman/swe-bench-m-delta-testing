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

QUARTO_ROOT: Final[str] = '/testbed'
TESTS_DIR: Final[str] = '/testbed/tests'
QUARTO_SHARE_PATH: Final[str] = '/testbed/src/resources'
QUARTO_BIN_PATH: Final[str] = '/testbed/package/dist/bin'
# Where tests/run-tests.sh finds deno has moved with quarto's packaging:
# bin/deno until spring 2022, bin/tools/deno after, and bin/tools/<arch>/
# deno once the arch-specific layout arrived. Hard-coding the middle one
# made every run of quarto-cli-475 exit 127 before a single test, leaving
# an empty result instead of a failure. First existing path wins.
DENO_CANDIDATES: Final[tuple[str, ...]] = (
    f'{QUARTO_BIN_PATH}/tools/x86_64/deno',
    f'{QUARTO_BIN_PATH}/tools/deno',
    f'{QUARTO_BIN_PATH}/deno',
)
# run-tests.sh switched its --importmap to dev_import_map.json when that
# file appeared; older checkouts only have import_map.json.
IMPORT_MAP_CANDIDATES: Final[tuple[str, ...]] = (
    '/testbed/src/dev_import_map.json',
    '/testbed/src/import_map.json',
)
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

        deno_bin = self._first_existing(DENO_CANDIDATES)
        import_map = self._first_existing(IMPORT_MAP_CANDIDATES)
        log.info('deno: %s  import map: %s', deno_bin, import_map)
        command = (
            f'{TIMEOUT_CMD} {deno_bin} test {DENO_TEST_FLAGS}'
            f' --importmap={import_map}'
        )
        exit_code, output = self.container.exec_run(
            command,
            workdir=TESTS_DIR,
            environment={
                'NO_COLOR': '1',
                # run-tests.sh exports QUARTO_ROOT alongside QUARTO_DEBUG.
                # With debug on and no root, quarto() looks for its dev
                # `configuration` file under src/, fails to read it, and
                # every smoke test dies there before rendering anything:
                # ~190 of 224 tests in both base and gold on quarto-cli
                # 2689, 2756, 3853, 4064 and 4184.
                'QUARTO_ROOT': QUARTO_ROOT,
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

    def _first_existing(self, paths: tuple[str, ...]) -> str:
        """The first of `paths` present in the container, else the last.

        Falling back to the last candidate keeps the old behaviour of a
        clear "No such file" in the log when none exist.
        """
        assert self.container is not None
        probe = ' '.join(f'[ -e {p} ] && echo {p} && exit 0;' for p in paths)
        _, output = self.container.exec_run(
            ['sh', '-c', probe + ' exit 1'], stream=False)
        assert isinstance(output, bytes)
        found = output.decode().strip().splitlines()
        return found[0] if found else paths[-1]

    @override
    def pre_cleanup(self) -> None:
        """Pre-cleanup hook. No-op for this evaluator."""
        pass

    @override
    def post_cleanup(self) -> None:
        """Post-cleanup hook. No-op for this evaluator."""
        pass
