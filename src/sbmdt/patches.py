"""Splitting a gold patch into its code and test halves.

SWE-bench scores a model patch against the maintainer's tests, so those
tests have to be present in the container no matter what the model
wrote. This project's ``gold_patch.diff`` bundles the code fix and the
new tests into a single diff, so applying it wholesale to a model run
would hand the model the reference fix, and applying nothing leaves the
FAIL_TO_PASS tests absent. Splitting it lets the two halves be applied
independently.

The split is by file path: anything that looks like a test file goes to
the test half, everything else to the code half. Section order is
preserved within each half, so both remain applicable with ``git apply``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from sbmdt.env import DOCKERFILES_BASE

__all__ = [
    'GOLD_PATCH_DIFF_FILENAME',
    'TEST_PATCH_DIFF_FILENAME',
    'CODE_PATCH_DIFF_FILENAME',
    'CODE_PATCH_PRED_FILENAME',
    'is_test_path',
    'split_diff',
    'test_patch_for',
    'write_diff',
]

GOLD_PATCH_DIFF_FILENAME: Final[str] = 'gold_patch.diff'
CODE_PATCH_DIFF_FILENAME: Final[str] = 'code_patch.diff'
TEST_PATCH_DIFF_FILENAME: Final[str] = 'test_patch.diff'
CODE_PATCH_PRED_FILENAME: Final[str] = 'code_patch.pred'

# A path is a test path if it sits in a test directory or carries a test
# suffix. Kept deliberately broad: misfiling a test file as code would
# silently reintroduce the very problem this module exists to solve.
TEST_PATH: Final[re.Pattern[str]] = re.compile(
    r"""
    (^|/)(test|tests|spec|specs|__tests__|__test__|e2e|cypress)/
    | [-_.](test|spec)\.[cm]?[jt]sx?$
    | \.(test|spec)\.[cm]?[jt]sx?$
    | (^|/)conftest\.py$
    | (^|/)test_[^/]*\.py$
    | [^/]*_test\.py$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Start of a per-file section in a unified diff.
DIFF_HEADER: Final[re.Pattern[str]] = re.compile(
    r'^diff --git a/(\S+) b/(\S+)', re.M
)


def is_test_path(path: str) -> bool:
    """Return True when ``path`` looks like a test file.

    Args:
        path: A repository-relative path taken from a diff header.

    Returns:
        Whether the path belongs to the test suite.
    """
    return TEST_PATH.search(path) is not None


def split_diff(diff: str) -> tuple[str, str]:
    """Split a unified diff into its non-test and test halves.

    The diff is cut at each ``diff --git`` header and every per-file
    section is routed by its path, so the two halves together contain
    exactly the sections of the original.

    Args:
        diff: The full unified diff.

    Returns:
        A ``(code_diff, test_diff)`` pair. Either may be empty.
    """
    starts = [m.start() for m in DIFF_HEADER.finditer(diff)]
    if not starts:
        return diff, ''

    code_parts: list[str] = []
    test_parts: list[str] = []
    bounds = starts + [len(diff)]
    for begin, end in zip(bounds[:-1], bounds[1:], strict=True):
        section = diff[begin:end]
        header = DIFF_HEADER.match(section)
        assert header is not None
        # b/ is the post-image path, which is the one that exists after
        # the patch applies; a/ is /dev/null for a newly added file.
        path = header.group(2)
        (test_parts if is_test_path(path) else code_parts).append(section)

    return ''.join(code_parts), ''.join(test_parts)


def read_diff(path: Path) -> str:
    """Read a diff off disk without mangling its bytes or line endings.

    Two things have to survive the round trip or ``git apply`` will
    reject the result. Patches routinely carry fixture files that are not
    valid UTF-8, hence ``surrogateescape``. And a patch's line endings
    are content rather than formatting: a diff of a CRLF file carries
    CRLF in its context lines, and one gold patch can mix both. Universal
    newline mode would collapse that distinction, so it is disabled here.

    Args:
        path: The diff file to read.

    Returns:
        The diff contents, byte for byte.
    """
    with open(
        path, encoding='utf-8', errors='surrogateescape', newline=''
    ) as handle:
        return handle.read()


def write_diff(path: Path, diff: str) -> None:
    """Write a diff back out with its line endings untouched.

    Disabling newline translation matters on Windows, where the default
    rewrites every line feed as CRLF and silently corrupts the context
    lines of any patch that was not CRLF to begin with.

    Args:
        path: Where to write the diff.
        diff: The diff contents.
    """
    with open(
        path, 'w', encoding='utf-8', errors='surrogateescape', newline=''
    ) as handle:
        handle.write(diff)


def test_patch_for(instance_id: str, base: Path = DOCKERFILES_BASE) -> str:
    """Return the test half of an instance's gold patch.

    Prefers a ``test_patch.diff`` written by ``scripts/split_gold_patch.py``
    and otherwise derives it from ``gold_patch.diff`` on the fly, so the
    evaluator works whether or not the split has been materialised. That
    matters on EC2 workers, which clone the repo and so have the committed
    ``gold_patch.diff`` but not the generated split.

    Args:
        instance_id: The benchmark instance to look up.
        base: Directory of per-instance folders.

    Returns:
        The test half of the patch, empty when the gold patch touches no
        test file.

    Raises:
        FileNotFoundError: If the instance has neither a test patch nor a
            gold patch to derive one from.
    """
    instance_dir = base / instance_id

    written = instance_dir / TEST_PATCH_DIFF_FILENAME
    if written.is_file():
        return read_diff(written)

    gold = instance_dir / GOLD_PATCH_DIFF_FILENAME
    if not gold.is_file():
        raise FileNotFoundError(
            f'{instance_id} has neither {TEST_PATCH_DIFF_FILENAME} nor '
            f'{GOLD_PATCH_DIFF_FILENAME} in {instance_dir}'
        )
    _, test_diff = split_diff(read_diff(gold))
    return test_diff
