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

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Final

from sbmdt.env import DOCKERFILES_BASE

log = logging.getLogger(__name__)

__all__ = [
    'GOLD_PATCH_DIFF_FILENAME',
    'TEST_PATCH_DIFF_FILENAME',
    'CODE_PATCH_DIFF_FILENAME',
    'CODE_PATCH_PRED_FILENAME',
    'is_test_path',
    'split_diff',
    'split_diff_by_file',
    'drop_unappliable_binary',
    'drop_mode_only_sections',
    'test_patch_for',
    'test_assets_for',
    'git_blob_id',
    'write_diff',
]

GOLD_PATCH_DIFF_FILENAME: Final[str] = 'gold_patch.diff'
CODE_PATCH_DIFF_FILENAME: Final[str] = 'code_patch.diff'
TEST_PATCH_DIFF_FILENAME: Final[str] = 'test_patch.diff'
CODE_PATCH_PRED_FILENAME: Final[str] = 'code_patch.pred'
# Binary files a test patch names but does not carry, recovered by
# scripts/fetch_test_assets.py.
TEST_ASSETS_DIR: Final[str] = 'test_assets'
TEST_ASSETS_MANIFEST: Final[str] = 'manifest.json'

# A path is a test path if it sits in a test directory or carries a test
# suffix. Kept deliberately broad: misfiling a test file as code would
# silently reintroduce the very problem this module exists to solve.
#
# `__snapshots__/` and the `.snap` suffix are here because leaving them
# out misfiled 13 files across 12 carbon instances. A jest snapshot is
# the recorded half of an assertion, so withholding it while applying
# the test that reads it leaves the suite checking new output against a
# stale expectation. Four of those instances -- carbon-3610, -4260,
# -4999 and -15197 -- had a snapshot as the *only* test-side file in
# their gold patch, so the split produced a 0-byte `test_patch.diff`
# and there was no test half at all.
#
# Note the suffix rules below all anchor on the end of the path, which
# is why `Dropdown-test.js.snap` slipped through every one of them: it
# ends in `.snap`, not in `-test.js`. Matching on the directory alone
# would not have caught it either, since these snapshots sit beside the
# component in `__snapshots__/` rather than under `__tests__/`.
#
# All 12 reported `f2p_total = 0` before this fix, and the first version
# of this comment concluded from that the routing was cosmetic. That was
# wrong, and the reason it was wrong is worth keeping.
#
# The zero was not evidence that a snapshot-only change is unmeasurable;
# it was an artefact of the bug. A `before_patch` run applies the test
# patch, so with the snapshot misfiled as source BOTH runs saw the stale
# snapshot and both passed -- no transition for delta testing to find.
# Withhold nothing and the pre-patch run checks new component output
# against the recorded old output, which is exactly what a snapshot test
# is for.
#
# Re-running the references settles it. carbon-3610, -4260 and -15197 go
# from `f2p_total = 0` to 3, and the three tests are the Dropdown cases
# that read the snapshot. So this moves score, and any instance in the
# list of 12 whose reference predates the fix is stale until re-run.
TEST_PATH: Final[re.Pattern[str]] = re.compile(
    r"""
    (^|/)(test|tests|spec|specs|__tests__|__test__|e2e|cypress)/
    | (^|/)__snapshots__/
    | [-_.](test|spec)\.[cm]?[jt]sx?$
    | \.(test|spec)\.[cm]?[jt]sx?$
    | \.snap$
    | (^|/)conftest\.py$
    | (^|/)test_[^/]*\.py$
    | [^/]*_test\.py$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# A binary change that the diff only names instead of carrying.
BINARY_STUB: Final[re.Pattern[str]] = re.compile(
    r'^Binary files .* differ$', re.M
)

# The marker that a binary change does carry its payload.
GIT_BINARY_PAYLOAD: Final[str] = 'GIT binary patch'

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


def split_diff_by_file(diff: str) -> list[tuple[str, str]]:
    """Cut a unified diff into one section per file.

    ``git apply`` is all-or-nothing: a single section it cannot place
    rejects the whole patch, which in practice means a model's real code
    change is thrown away because the same submission also touched a
    lockfile the image had already regenerated. Applying file by file
    lets the caller keep what does apply and report what does not.

    Args:
        diff: The full unified diff.

    Returns:
        ``(path, section)`` pairs in the order they appear, where ``path``
        is the post-image (``b/``) path. Empty when the text carries no
        ``diff --git`` header at all.
    """
    starts = [m.start() for m in DIFF_HEADER.finditer(diff)]
    if not starts:
        return []

    out: list[tuple[str, str]] = []
    bounds = starts + [len(diff)]
    for begin, end in zip(bounds[:-1], bounds[1:], strict=True):
        section = diff[begin:end]
        header = DIFF_HEADER.match(section)
        assert header is not None
        out.append((header.group(2), section))
    return out


def drop_unappliable_binary(diff: str) -> tuple[str, list[str]]:
    """Drop sections that declare a binary change without carrying it.

    ``git diff`` embeds binary content only when asked with ``--binary``;
    without it a changed binary file becomes a bare ``Binary files a/x
    and b/x differ`` line. ``git apply`` refuses such a section ("cannot
    apply binary patch ... without full index line"), and no retry can
    succeed, because the bytes are not in the patch to begin with. One
    such section fails the entire patch and so the whole instance, which
    is a worse outcome than proceeding without a file the diff never
    contained.

    Args:
        diff: A unified diff.

    Returns:
        A ``(filtered_diff, dropped_paths)`` pair.
    """
    starts = [m.start() for m in DIFF_HEADER.finditer(diff)]
    if not starts:
        return diff, []

    kept: list[str] = []
    dropped: list[str] = []
    bounds = starts + [len(diff)]
    for begin, end in zip(bounds[:-1], bounds[1:], strict=True):
        section = diff[begin:end]
        header = DIFF_HEADER.match(section)
        assert header is not None
        if BINARY_STUB.search(section) and GIT_BINARY_PAYLOAD not in section:
            dropped.append(header.group(2))
        else:
            kept.append(section)

    return ''.join(kept), dropped


def drop_mode_only_sections(diff: str) -> tuple[str, list[str]]:
    """Remove pure executable-bit changes from a model prediction.

    Some Refact outputs contain hundreds of ``old mode 100644`` / ``new
    mode 100755`` sections. They carry no source edit and are frequently
    incompatible with the benchmark checkout's file-mode metadata. Removing
    such a section is safe because it cannot change repository contents.
    Sections with a normal unified hunk are retained even when they also
    change a mode.
    """
    starts = [m.start() for m in DIFF_HEADER.finditer(diff)]
    if not starts:
        return diff, []
    kept: list[str] = []
    dropped: list[str] = []
    bounds = starts + [len(diff)]
    for begin, end in zip(bounds[:-1], bounds[1:], strict=True):
        section = diff[begin:end]
        header = DIFF_HEADER.match(section)
        assert header is not None
        has_mode = bool(
            re.search(r'^old mode \d+\nnew mode \d+', section, re.M)
        )
        has_content = bool(re.search(r'^(--- |\+\+\+ |@@ )', section, re.M))
        if has_mode and not has_content:
            dropped.append(header.group(2))
        else:
            kept.append(section)
    return ''.join(kept), dropped


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

    # An empty materialised split is treated as absent rather than as an
    # answer. `test_patch.diff` is a cache of `split_diff(gold)`, and a
    # cache must never be able to be more wrong than what it caches --
    # but `is_file()` is true for a zero-byte file, so a stale one wins
    # silently and the run gets no tests at all.
    #
    # That is not hypothetical: the four carbon instances whose only
    # test-side file was a `.snap` were split before the snapshot rule
    # existed, so they have a committed 0-byte `test_patch.diff`. Fixing
    # `TEST_PATH` alone left them exactly as broken, because this branch
    # never reached the gold patch to re-derive anything.
    #
    # Falling through costs nothing when the empty half is genuine: the
    # derivation returns the same empty string, just computed rather
    # than remembered.
    written = instance_dir / TEST_PATCH_DIFF_FILENAME
    if written.is_file() and written.stat().st_size > 0:
        test_diff = read_diff(written)
    else:
        gold = instance_dir / GOLD_PATCH_DIFF_FILENAME
        if not gold.is_file():
            raise FileNotFoundError(
                f'{instance_id} has neither {TEST_PATCH_DIFF_FILENAME} nor '
                f'{GOLD_PATCH_DIFF_FILENAME} in {instance_dir}'
            )
        _, test_diff = split_diff(read_diff(gold))

    test_diff, dropped = drop_unappliable_binary(test_diff)
    if dropped:
        log.info(
            f'{instance_id}: dropped {len(dropped)} binary section(s) the '
            f'patch names but does not carry: {dropped}'
        )
    return test_diff


def git_blob_id(data: bytes) -> str:
    """The id git gives ``data`` as a blob (what a diff's index line names)."""
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def test_assets_for(
    instance_id: str, base: Path = DOCKERFILES_BASE
) -> list[tuple[str, bytes | None]]:
    """Binary files the test patch changes but ``git apply`` cannot write.

    ``drop_unappliable_binary`` removes a ``Binary files ... differ`` stub
    from the test patch because the bytes are not in it. For openlayers
    those stubs are the ``expected.png`` of every rendering case a fix
    adds or updates, so dropping them silently leaves the rendering test
    comparing against a stale image or none at all.
    ``scripts/fetch_test_assets.py`` recovers the exact blobs from the
    upstream pull request; this returns them for the evaluator to write
    after the text half of the test patch applies.

    A file is returned only when its bytes still hash to the blob id the
    manifest recorded, so a corrupted or hand-edited asset is never used.

    Args:
        instance_id: The benchmark instance to look up.
        base: Directory of per-instance folders.

    Returns:
        ``(repo path, bytes)`` pairs; ``bytes`` is ``None`` for a file the
        test patch deletes. Empty when the instance has no assets.
    """
    assets = base / instance_id / TEST_ASSETS_DIR
    manifest_file = assets / TEST_ASSETS_MANIFEST
    if not manifest_file.is_file():
        return []
    out: list[tuple[str, bytes | None]] = []
    for path, entry in sorted(json.loads(manifest_file.read_text()).items()):
        if entry['action'] == 'delete':
            out.append((path, None))
            continue
        data = (assets / entry['blob']).read_bytes()
        if git_blob_id(data) != entry['blob']:
            log.warning(
                f'{instance_id}: test asset {path} does not match blob '
                f'{entry["blob"]}; not restoring it'
            )
            continue
        out.append((path, data))
    return out
