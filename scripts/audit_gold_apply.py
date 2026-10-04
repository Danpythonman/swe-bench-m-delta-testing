"""Check that every scraped SWE-bench Verified gold patch applies.

Gold patches are scraped from GitHub PRs rather than taken from the
dataset, and a PR's diff is computed against its merge base, which can
drift from the base commit the benchmark checked out (a PR updated after
it was opened, say). Such an instance fails only when it is evaluated,
on a worker, at the cost of an image pull. This audit finds them up
front, without Docker.

Each repository is cloned once, blobless, under ``.cache/repos``. For
each instance the base commit's tree is read into a throwaway index and
the patch halves are checked against it with ``git apply --cached
--check``, which fetches only the blobs the patch touches. The same
fallbacks ``Evaluator.apply_patch`` uses are tried in order, so the
result says which one an evaluation will end up needing.

Usage:
    uv run scripts/audit_gold_apply.py
    uv run scripts/audit_gold_apply.py --instance django__django-11099
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Final

from sbmdt.benchmark import Benchmark, benchmark_of
from sbmdt.env import DOCKERFILES_BASE, PROJECT_BASE
from sbmdt.instance import InstanceMetadata
from sbmdt.log import setup_logging
from sbmdt.patches import (
    CODE_PATCH_DIFF_FILENAME,
    GOLD_PATCH_DIFF_FILENAME,
    TEST_PATCH_DIFF_FILENAME,
)

log = logging.getLogger(__name__)

REPOS_DIR: Final[Path] = PROJECT_BASE / '.cache' / 'repos'

# The fallbacks Evaluator.apply_patch tries, in the same order.
ATTEMPTS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ('clean', ()),
    ('recount', ('--recount',)),
    ('3way', ('--3way', '--whitespace=nowarn')),
)

PATCHES: Final[tuple[str, ...]] = (
    GOLD_PATCH_DIFF_FILENAME,
    CODE_PATCH_DIFF_FILENAME,
    TEST_PATCH_DIFF_FILENAME,
)


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


def check_patch(clone: Path, commit: str, patch: Path) -> str:
    """Return the first fallback under which ``patch`` applies at ``commit``.

    Args:
        clone: The repository clone.
        commit: The commit to apply against.
        patch: The patch file.

    Returns:
        ``'empty'``, an attempt name from :data:`ATTEMPTS`, or
        ``'fails'``.
    """
    if not patch.read_text(errors='replace').strip():
        return 'empty'
    with tempfile.TemporaryDirectory() as tmp:
        env = {'GIT_INDEX_FILE': str(Path(tmp) / 'index')}
        for name, flags in ATTEMPTS[:-1]:
            git(clone, 'read-tree', commit, env=env)
            try:
                git(
                    clone,
                    'apply',
                    '--cached',
                    '--check',
                    *flags,
                    str(patch.resolve()),
                    env=env,
                )
            except subprocess.CalledProcessError:
                continue
            return name
    return check_three_way(clone, commit, patch)


def check_three_way(clone: Path, commit: str, patch: Path) -> str:
    """Try the final ``--3way`` fallback in a throwaway worktree.

    A three-way apply needs the pre-image blobs the patch names, which a
    blobless clone lacks and ``--cached`` will not fetch. Applying in a
    real worktree fetches them on demand, matching the container, whose
    checkout has full history.

    Args:
        clone: The repository clone.
        commit: The commit to apply against.
        patch: The patch file.

    Returns:
        The last attempt name from :data:`ATTEMPTS`, or ``'fails'``.
    """
    name, flags = ATTEMPTS[-1]
    with tempfile.TemporaryDirectory() as tmp:
        worktree = Path(tmp) / 'worktree'
        git(
            clone,
            'worktree',
            'add',
            '--quiet',
            '--detach',
            str(worktree),
            commit,
        )
        try:
            git(worktree, 'apply', *flags, str(patch.resolve()))
        except subprocess.CalledProcessError:
            return 'fails'
        finally:
            git(clone, 'worktree', 'remove', '--force', str(worktree))
    return name


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(
        description='Check that scraped Verified gold patches apply.'
    )
    parser.add_argument(
        '--instance',
        action='append',
        default=None,
        help='Only audit this instance ID. Repeatable.',
    )
    parser.add_argument(
        '--out',
        type=Path,
        default=PROJECT_BASE / 'analysis' / 'verified' / 'gold_apply.csv',
        help='CSV to write per-instance results to.',
    )
    return parser.parse_args()


def main() -> None:
    """Audit every requested instance and write the CSV."""
    args = parse_args()
    setup_logging(level=logging.INFO)

    instance_ids = args.instance or sorted(
        path.name
        for path in DOCKERFILES_BASE.iterdir()
        if benchmark_of(path.name) == Benchmark.SWE_BENCH_VERIFIED
    )
    rows: list[dict[str, str]] = []
    for instance_id in instance_ids:
        metadata = InstanceMetadata.load(instance_id)
        clone = ensure_clone(metadata.repo)
        ensure_commit(clone, metadata.base_commit)
        row = {'instance_id': instance_id}
        for filename in PATCHES:
            row[filename] = check_patch(
                clone,
                metadata.base_commit,
                DOCKERFILES_BASE / instance_id / filename,
            )
        if row[GOLD_PATCH_DIFF_FILENAME] != 'clean':
            log.warning(f'{instance_id}: {row}')
        rows.append(row)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, 'w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    for filename in PATCHES:
        counts: dict[str, int] = {}
        for row in rows:
            counts[row[filename]] = counts.get(row[filename], 0) + 1
        log.info(f'{filename}: {dict(sorted(counts.items()))}')
    log.info(f'wrote {args.out}')


if __name__ == '__main__':
    main()
