"""Recover the binary files that the test patches name but do not carry.

The released diffs record a changed binary file as a bare ``Binary files
a/x and b/x differ`` line, so ``git apply`` cannot write it and
``patches.drop_unappliable_binary`` drops the section. For openlayers
that loses every ``test/rendering/cases/*/expected.png`` a fix adds or
updates -- the whole oracle of a rendering test -- and leaves 32
instances whose test patch is nothing else ungradeable.

The bytes are not lost, only elsewhere. Each stub keeps its ``index
<old>..<new>`` line, i.e. the git blob id of the file the patch meant to
write, and the instance number is the upstream pull request. This script
asks GitHub for that pull request's files, keeps a file only when its
blob id matches the stub's, re-checks the id over the downloaded bytes,
and stores the result where ``patches.test_assets_for`` finds it:

    dockerfiles/<instance>/test_assets/manifest.json
    dockerfiles/<instance>/test_assets/<blob id>

A stub that deletes a file needs no bytes and is recorded as a delete.
Anything that cannot be matched to the exact blob is left out, so a
restored file is always byte-identical to the one the patch describes.

Needs an authenticated ``gh`` CLI (public-repo reads only).

Usage:
    uv run scripts/fetch_test_assets.py                 # every instance
    uv run scripts/fetch_test_assets.py --instance <id>
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from sbmdt.env import DOCKERFILES_BASE  # noqa: E402
from sbmdt.patches import (  # noqa: E402
    BINARY_STUB,
    GIT_BINARY_PAYLOAD,
    TEST_ASSETS_DIR,
    TEST_ASSETS_MANIFEST,
    git_blob_id,
    read_diff,
)

UPSTREAM = {
    'openlayers': 'openlayers/openlayers',
}

SECTION = re.compile(r'^diff --git a/(\S+) b/(\S+)$', re.M)
INDEX = re.compile(r'^index ([0-9a-f]+)\.\.([0-9a-f]+)', re.M)


def stubs(diff: str) -> list[dict]:
    """Binary sections of ``diff`` that name a change without its bytes."""
    starts = [m.start() for m in SECTION.finditer(diff)] + [len(diff)]
    out = []
    for begin, end in zip(starts[:-1], starts[1:], strict=True):
        section = diff[begin:end]
        if not BINARY_STUB.search(section) or GIT_BINARY_PAYLOAD in section:
            continue
        header = SECTION.match(section)
        index = INDEX.search(section)
        if header is None or index is None:
            continue
        old, new = index.groups()
        if set(new) == {'0'}:
            action = 'delete'
        elif set(old) == {'0'}:
            action = 'add'
        else:
            action = 'modify'
        out.append({'path': header.group(2), 'action': action, 'blob': new})
    return out


def gh_json(path: str) -> list:
    result = subprocess.run(
        ['gh', 'api', '--paginate', path],
        capture_output=True,
        check=True,
    )
    # --paginate concatenates JSON arrays back to back.
    text = result.stdout.decode().replace('][', ',')
    return json.loads(text)


def gh_blob(repo: str, sha: str) -> bytes:
    result = subprocess.run(
        [
            'gh', 'api', '-H', 'Accept: application/vnd.github.raw',
            f'repos/{repo}/git/blobs/{sha}',
        ],
        capture_output=True,
        check=True,
    )
    return result.stdout


def fetch(instance_dir: Path) -> tuple[int, int, list[str]]:
    """Materialise one instance's assets.

    Returns:
        (restored, deleted, unmatched paths)
    """
    diff_file = instance_dir / 'test_patch.diff'
    if not diff_file.is_file():
        return 0, 0, []
    wanted = stubs(read_diff(diff_file))
    if not wanted:
        return 0, 0, []

    owner = instance_dir.name.split('__')[0]
    repo = UPSTREAM.get(owner)
    number = instance_dir.name.rsplit('-', 1)[1]
    files = {}
    if repo and any(w['action'] != 'delete' for w in wanted):
        for entry in gh_json(f'repos/{repo}/pulls/{number}/files'):
            files[entry['filename']] = entry['sha']

    assets = instance_dir / TEST_ASSETS_DIR
    manifest, restored, deleted, missing = {}, 0, 0, []
    for w in wanted:
        if w['action'] == 'delete':
            manifest[w['path']] = {'action': 'delete'}
            deleted += 1
            continue
        sha = files.get(w['path'], '')
        if not sha.startswith(w['blob']):
            missing.append(w['path'])
            continue
        data = gh_blob(repo, sha)
        if git_blob_id(data) != sha:
            missing.append(w['path'])
            continue
        assets.mkdir(parents=True, exist_ok=True)
        (assets / sha).write_bytes(data)
        manifest[w['path']] = {'action': w['action'], 'blob': sha}
        restored += 1

    if manifest:
        assets.mkdir(parents=True, exist_ok=True)
        (assets / TEST_ASSETS_MANIFEST).write_text(
            json.dumps(manifest, indent=1, sort_keys=True) + '\n'
        )
    return restored, deleted, missing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--instance', help='only this instance')
    args = parser.parse_args()

    dirs = sorted(
        p for p in DOCKERFILES_BASE.iterdir()
        if p.is_dir() and (not args.instance or p.name == args.instance)
    )
    total_r = total_d = 0
    for instance_dir in dirs:
        restored, deleted, missing = fetch(instance_dir)
        if restored or deleted or missing:
            print(f'{instance_dir.name}: restored {restored}, '
                  f'deletes {deleted}, unmatched {len(missing)} {missing}')
        total_r += restored
        total_d += deleted
    print(f'total: restored {total_r}, deletes {total_d}')


if __name__ == '__main__':
    main()
