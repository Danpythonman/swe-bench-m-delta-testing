"""Normalization of the test file paths runners report."""

from __future__ import annotations

from typing import Final

__all__ = [
    'repo_relative',
]

REPO_ROOT: Final[str] = '/testbed/'


def repo_relative(path: str | None) -> str | None:
    """Return ``path`` relative to the repository root in the container.

    Runners report a test's file as an absolute path (Jest), a path
    relative to their working directory, or not at all. Benchmark labels
    use repository-relative paths, so this strips the ``/testbed/`` prefix
    and any leading ``./``.

    Args:
        path: The path as the runner reported it, or ``None``.

    Returns:
        The repository-relative path, or ``None`` when ``path`` is empty.
    """
    if not path:
        return None
    path = path.strip()
    if path.startswith(REPO_ROOT):
        path = path[len(REPO_ROOT) :]
    while path.startswith('./'):
        path = path[2:]
    return path or None
