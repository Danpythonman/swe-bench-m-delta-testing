"""Create instance directories for SWE-bench Verified.

Reads the SWE-bench Verified dataset (as data only; none of the SWE-bench
harness code is used) and, for every instance, writes into
``dockerfiles/<instance_id>/``:

    Dockerfile        a lean image that only names the prebuilt
                      ``sweb.eval`` environment through the ECR cache
    instance.json     repository, version, and base commit
    reference.json    the published FAIL_TO_PASS / PASS_TO_PASS lists,
                      held out for scripts/compare_official_split.py

The dataset's own ``patch`` and ``test_patch`` columns are deliberately
ignored. Gold patches are scraped from the GitHub PRs afterwards, the same
way as for SWE-bench M:

    uv run scripts/import_swebench.py
    GITHUB_TOKEN=... uv run scripts/get_gold_patches.py
    uv run scripts/split_gold_patch.py

Usage:
    uv run scripts/import_swebench.py                       # every instance
    uv run scripts/import_swebench.py --instance <id> ...   # just these
    uv run scripts/import_swebench.py --dataset-file x.parquet
"""

from __future__ import annotations

import argparse
import json
import logging
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Final

import pyarrow.parquet as pq

from sbmdt.benchmark import Benchmark, benchmark_of
from sbmdt.env import DOCKERFILES_BASE
from sbmdt.instance import InstanceMetadata, ReferenceSplit
from sbmdt.log import setup_logging

log = logging.getLogger(__name__)

DATASET_URL: Final[str] = (
    'https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified/'
    'resolve/main/data/test-00000-of-00001.parquet'
)

# Docker Hub images are pulled through this ECR pull-through cache to avoid
# Docker Hub's anonymous rate limit (see aws/run_ec2.sh).
ECR_DOCKER_HUB: Final[str] = (
    '607869540801.dkr.ecr.us-east-1.amazonaws.com/docker-hub'
)

DOCKERFILE_TEMPLATE: Final[str] = """\
FROM {registry}/swebench/sweb.eval.x86_64.{image}:latest

WORKDIR /testbed
"""


def image_name(instance_id: str) -> str:
    """Return the prebuilt image name for ``instance_id``.

    Docker repository names cannot contain ``__``, so the published
    images spell the owner/repo separator as ``_1776_``, lowercased.

    Args:
        instance_id: e.g. ``'django__django-11099'``.

    Returns:
        e.g. ``'django_1776_django-11099'``.
    """
    return instance_id.replace('__', '_1776_').lower()


def load_rows(dataset_file: Path | None) -> list[dict[str, Any]]:
    """Load the dataset rows, downloading the parquet file if needed.

    Args:
        dataset_file: A local copy of the dataset, or ``None`` to
            download it from Hugging Face.

    Returns:
        One dict per instance.
    """
    if dataset_file is not None:
        return pq.read_table(dataset_file).to_pylist()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'verified.parquet'
        log.info(f'Downloading {DATASET_URL}')
        urllib.request.urlretrieve(DATASET_URL, path)
        return pq.read_table(path).to_pylist()


def import_row(row: dict[str, Any], base: Path) -> None:
    """Write one instance's Dockerfile, instance.json, and reference.json.

    Existing files are overwritten; scraped gold patches and anything
    derived from them are left alone.

    Args:
        row: One dataset row.
        base: Directory of per-instance folders.

    Raises:
        ValueError: If the instance's repository is not a known Verified
            repository, which would make it dispatch as SWE-bench M.
    """
    instance_id: str = row['instance_id']
    if benchmark_of(instance_id) != Benchmark.SWE_BENCH_VERIFIED:
        raise ValueError(
            f'{instance_id}: repository not in SWE_BENCH_VERIFIED_PREFIXES'
        )

    instance_dir = base / instance_id
    instance_dir.mkdir(parents=True, exist_ok=True)
    (instance_dir / 'Dockerfile').write_text(
        DOCKERFILE_TEMPLATE.format(
            registry=ECR_DOCKER_HUB, image=image_name(instance_id)
        )
    )
    InstanceMetadata(
        instance_id=instance_id,
        repo=row['repo'],
        version=str(row['version']),
        base_commit=row['base_commit'],
    ).write(base)
    # The dataset stores both lists as JSON-encoded strings.
    ReferenceSplit(
        instance_id=instance_id,
        fail_to_pass=json.loads(row['FAIL_TO_PASS']),
        pass_to_pass=json.loads(row['PASS_TO_PASS']),
    ).write(base)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(
        description='Create instance directories for SWE-bench Verified.'
    )
    parser.add_argument(
        '--instance',
        action='append',
        default=None,
        help='Only import this instance ID. Repeatable.',
    )
    parser.add_argument(
        '--dataset-file',
        type=Path,
        default=None,
        help='Local copy of the dataset parquet (default: download it).',
    )
    parser.add_argument(
        '--dockerfiles',
        type=Path,
        default=DOCKERFILES_BASE,
        help='Directory of per-instance folders.',
    )
    return parser.parse_args()


def main() -> None:
    """Import every requested instance."""
    args = parse_args()
    setup_logging(level=logging.INFO)

    rows = load_rows(args.dataset_file)
    if args.instance:
        wanted = set(args.instance)
        rows = [row for row in rows if row['instance_id'] in wanted]
        missing = wanted - {row['instance_id'] for row in rows}
        if missing:
            raise SystemExit(f'not in dataset: {sorted(missing)}')

    for row in rows:
        import_row(row, args.dockerfiles)
    log.info(f'Imported {len(rows)} instance(s) into {args.dockerfiles}')


if __name__ == '__main__':
    main()
