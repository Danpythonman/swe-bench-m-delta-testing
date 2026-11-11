"""Blobless clones of benchmark repositories, cached under ``.cache/repos``.

Used to inspect a repository at an instance's base commit without Docker:
checking that a gold patch applies, reading a test file as the patch left
it. Each repository is cloned once, without blobs; git fetches the blobs a
command needs on demand.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Final

from sbmdt.env import PROJECT_BASE

__all__ = [
    'REPOS_DIR',
    'ensure_clone',
    'ensure_commit',
    'git',
]

log = logging.getLogger(__name__)

REPOS_DIR: Final[Path] = PROJECT_BASE / '.cache' / 'repos'


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """Run git in ``repo`` and return its stdout.

    Raises:
        subprocess.CalledProcessError: If git exits non-zero.
    """
    return subprocess.run(
        ['git', '-C', str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    ).stdout


def ensure_clone(repo: str) -> Path:
    """Return a blobless clone of ``repo`` (``owner/name``), cloning once.

    Args:
        repo: The GitHub repository.

    Returns:
        The clone's path.
    """
    path = REPOS_DIR / repo.replace('/', '__')
    if not path.is_dir():
        log.info(f'Cloning {repo} (blobless)')
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                'git',
                'clone',
                '--quiet',
                '--filter=blob:none',
                '--no-checkout',
                f'https://github.com/{repo}.git',
                str(path),
            ],
            check=True,
        )
    return path


def ensure_commit(clone: Path, commit: str) -> None:
    """Fetch ``commit`` into ``clone`` if it is not already there.

    Base commits are usually on the default branch, but not always.
    """
    try:
        git(clone, 'cat-file', '-e', f'{commit}^{{commit}}')
    except subprocess.CalledProcessError:
        git(clone, 'fetch', '--quiet', '--filter=blob:none', 'origin', commit)
