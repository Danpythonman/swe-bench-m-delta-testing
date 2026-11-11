"""Which version of the harness produced a result.

Every :class:`~sbmdt.evaluator.base.TestResult` carries the commit it was
produced with, so results from different versions of an evaluator can be
told apart. Without it, a reference split silently mixed runs from two
branches: the fixes on one were missing from the other's runs, and nothing
in the results said which runs were which.
"""

from __future__ import annotations

import functools
import os
import subprocess
from typing import Final

from sbmdt.env import PROJECT_BASE

__all__ = [
    'COMMIT_ENV',
    'harness_commit',
]

# Set by a caller that knows the commit better than the checkout does.
COMMIT_ENV: Final[str] = 'SBMDT_COMMIT'

# What decides a result: the code, the instance data, and the environment.
_TRACKED: Final[tuple[str, ...]] = (
    'src',
    'scripts',
    'dockerfiles',
    'aws',
    'pyproject.toml',
    'uv.lock',
)


@functools.cache
def harness_commit() -> str | None:
    """The git commit of this checkout, suffixed ``-dirty`` when modified.

    ``$SBMDT_COMMIT`` takes precedence. Otherwise the commit is read from
    the checkout; a tracked change under ``_TRACKED`` marks it ``-dirty``,
    because then the commit alone does not say what ran.

    Returns:
        e.g. ``96accd7...`` or ``96accd7...-dirty``; ``None`` outside a git
        checkout.
    """
    if commit := os.environ.get(COMMIT_ENV):
        return commit
    # SSM runs as root in a checkout another user cloned, which git refuses
    # ("dubious ownership") unless the directory is marked safe.
    git = [
        'git',
        '-c',
        f'safe.directory={PROJECT_BASE}',
        '-C',
        str(PROJECT_BASE),
    ]
    try:
        head = subprocess.run(
            [*git, 'rev-parse', 'HEAD'],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        changes = subprocess.run(
            [
                *git,
                'status',
                '--porcelain',
                '--untracked-files=no',
                '--',
                *_TRACKED,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return f'{head}-dirty' if changes else head
