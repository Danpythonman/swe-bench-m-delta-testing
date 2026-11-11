"""Choose which tests a Python evaluation runs.

Python instances run in *touched* scope: only the tests the instance's
test patch adds or modifies, rather than the whole suite. The starting
point is the set of files ``test_patch.diff`` changes; each evaluator
then turns those paths into whatever its runner accepts (pytest paths,
Django module labels, ...).
"""

from __future__ import annotations

import configparser
import fnmatch
import re
import tomllib
from typing import Any, Final, cast

from sbmdt.patches import DIFF_HEADER

__all__ = [
    'DEFAULT_PYTHON_FILES',
    'PYTEST_CONFIG_FILES',
    'changed_paths',
    'is_python_test_module',
    'matches_python_files',
    'python_files_patterns',
]

# pytest's default ``python_files``.
DEFAULT_PYTHON_FILES: Final[tuple[str, ...]] = ('test_*.py', '*_test.py')

# Files pytest reads its configuration from, in the order it looks for
# them. The first one that carries a pytest section wins (pytest.ini wins
# even without one).
PYTEST_CONFIG_FILES: Final[tuple[str, ...]] = (
    'pytest.ini',
    'pyproject.toml',
    'tox.ini',
    'setup.cfg',
)

# The ini section pytest reads in each file other than pyproject.toml.
_INI_SECTIONS: Final[dict[str, str]] = {
    'pytest.ini': 'pytest',
    'tox.ini': 'pytest',
    'setup.cfg': 'tool:pytest',
}

# A file that pytest's default discovery treats as a test module.
PYTHON_TEST_MODULE: Final[re.Pattern[str]] = re.compile(
    r'(^|/)(test_[^/]*|[^/]*_test)\.py$'
)


def changed_paths(diff: str) -> list[str]:
    """Return the paths a diff leaves in place, in diff order.

    Files the diff deletes are excluded, since there is nothing left to
    run. Each path appears once.

    Args:
        diff: A unified diff.

    Returns:
        Repository-relative post-image paths.
    """
    starts = [m.start() for m in DIFF_HEADER.finditer(diff)]
    bounds = starts + [len(diff)]
    paths: list[str] = []
    for begin, end in zip(bounds[:-1], bounds[1:], strict=True):
        section = diff[begin:end]
        header = DIFF_HEADER.match(section)
        assert header is not None
        deleted = re.search(
            r'^(deleted file mode|\+\+\+ /dev/null)', section, re.M
        )
        path = header.group(2)
        if not deleted and path not in paths:
            paths.append(path)
    return paths


def is_python_test_module(path: str) -> bool:
    """Return True when ``path`` is a Python test module.

    Args:
        path: A repository-relative path.

    Returns:
        Whether pytest's default discovery would collect it.
    """
    return PYTHON_TEST_MODULE.search(path) is not None


def _ini_python_files(name: str, text: str) -> list[str] | None:
    """Return ``python_files`` from an ini-style config, if it has one.

    Returns:
        The patterns, ``[]`` when the pytest section exists without the
        option, or ``None`` when the file has no pytest section (so pytest
        would keep looking), except that pytest.ini always counts.
    """
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    section = _INI_SECTIONS[name]
    if not parser.has_section(section):
        return [] if name == 'pytest.ini' else None
    return parser.get(section, 'python_files', fallback='').split()


def _toml_python_files(text: str) -> list[str] | None:
    """Return ``python_files`` from pyproject.toml, if it configures pytest.

    Returns:
        The patterns, ``[]`` when ``[tool.pytest.ini_options]`` exists
        without the option, or ``None`` when it does not exist.
    """
    try:
        data: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    options: Any = data.get('tool', {}).get('pytest', {}).get('ini_options')
    if not isinstance(options, dict):
        return None
    value = cast(dict[str, Any], options).get('python_files', [])
    if isinstance(value, str):
        return value.split()
    return [str(item) for item in cast(list[Any], value)]


def python_files_patterns(configs: dict[str, str]) -> tuple[str, ...]:
    """Return the ``python_files`` patterns a repository configures.

    Follows pytest's own lookup: the first file in
    :data:`PYTEST_CONFIG_FILES` that configures pytest decides, and when
    it does not set ``python_files`` the default applies.

    Args:
        configs: Contents of whichever of :data:`PYTEST_CONFIG_FILES` the
            repository root has, keyed by file name.

    Returns:
        The patterns test modules must match.
    """
    for name in PYTEST_CONFIG_FILES:
        if name not in configs:
            continue
        if name == 'pyproject.toml':
            patterns = _toml_python_files(configs[name])
        else:
            patterns = _ini_python_files(name, configs[name])
        if patterns is not None:
            return tuple(patterns) or DEFAULT_PYTHON_FILES
    return DEFAULT_PYTHON_FILES


def matches_python_files(path: str, patterns: tuple[str, ...]) -> bool:
    """Return True when pytest would treat ``path`` as a test module.

    Mirrors pytest's matching: a pattern without ``/`` is matched against
    the file name, one with ``/`` against the whole relative path.

    Args:
        path: A repository-relative path.
        patterns: ``python_files`` patterns.

    Returns:
        Whether ``path`` ends in ``.py`` and matches a pattern.
    """
    if not path.endswith('.py'):
        return False
    name = path.rpartition('/')[2]
    return any(
        fnmatch.fnmatch(path if '/' in pattern else name, pattern)
        for pattern in patterns
    )
