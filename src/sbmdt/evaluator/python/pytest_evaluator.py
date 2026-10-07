"""Evaluator for SWE-bench Verified repositories tested with pytest."""

from __future__ import annotations

import logging
import shlex
from typing import override

from sbmdt.evaluator.python.python import PythonEvaluator
from sbmdt.evaluator.python.selection import (
    PYTEST_CONFIG_FILES,
    matches_python_files,
    python_files_patterns,
)
from sbmdt.utils import read_from_container

__all__ = [
    'PytestEvaluator',
]

log = logging.getLogger(__name__)


class PytestEvaluator(PythonEvaluator):
    """Runs the touched test modules with pytest.

    Which touched files are test modules is decided by the repository's
    own ``python_files`` setting, read from its pytest configuration, so
    conventions like pylint's ``unittest_*.py`` or pytest's own
    ``testing/*/*.py`` are honoured without special cases, and fixture
    files (``conftest.py``, test data) are left for pytest to load as it
    normally would.

    The repository's pytest configuration is otherwise left in force too,
    since it is part of how the project's tests are meant to behave. The
    cache provider is disabled so a run never writes ``.pytest_cache``
    into the working tree.
    """

    def _pytest_configs(self) -> dict[str, str]:
        """Read whichever pytest config files the repository root has.

        Returns:
            File contents keyed by file name.

        Raises:
            RuntimeError: If the container has not been started.
        """
        if self.container is None:
            raise RuntimeError('Container not initialized')
        configs: dict[str, str] = {}
        for name in PYTEST_CONFIG_FILES:
            try:
                configs[name] = read_from_container(
                    self.container, f'/testbed/{name}'
                )
            except Exception:
                continue
        return configs

    @override
    def test_command(self, paths: list[str]) -> str | None:
        """Run every touched file the repository counts as a test module.

        Args:
            paths: Files the instance's test patch adds or modifies.

        Returns:
            A pytest invocation, or ``None`` when no path is a test module.
        """
        patterns = python_files_patterns(self._pytest_configs())
        log.info(f'python_files patterns: {patterns}')
        modules = [p for p in paths if matches_python_files(p, patterns)]
        if not modules:
            return None
        return ' '.join(
            [
                'python -m pytest',
                '-p sbmdt_pytest_reporter',
                '-p no:cacheprovider',
                *map(shlex.quote, modules),
            ]
        )
