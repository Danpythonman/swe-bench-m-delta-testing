"""Print the SWE-bench Verified pred keys an EC2 batch should run next.

Selects ``gold`` and ``before_patch`` keys from the preds bucket for
Verified instances, skipping runs that already have a result in the
results bucket, and optionally instances whose base image is not yet in
the ECR pull-through cache (see ``scripts/warm_ecr_cache.py``). The output
is meant for ``run_all_ec2.py --pred-keys``, which makes a large batch
resumable and lets it proceed in waves while the cache is being warmed.

Usage:
    uv run scripts/run_all_ec2.py --apply-test-patch \\
        --git-branch feature/swe-bench-verified --n-concurrent 62 \\
        --pred-keys $(uv run scripts/verified_pred_keys.py --cached-only)
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import boto3

from sbmdt.aws.ecr import base_image, is_cached
from sbmdt.aws.env import AWS_PROFILE, REGION
from sbmdt.aws.s3 import (
    PREDS_S3_BUCKET_NAME,
    TEST_RESULTS_S3_BUCKET_NAME,
    S3PredFilename,
)
from sbmdt.benchmark import Benchmark, benchmark_of
from sbmdt.evaluator.base import PatchType, TestResultsFilename

PATCH_TYPES = (PatchType.GOLD, PatchType.BEFORE_PATCH)


def list_keys(s3: Any, bucket: str) -> list[str]:
    """Return every key in ``bucket``."""
    keys: list[str] = []
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket):
        keys.extend(obj['Key'] for obj in page.get('Contents', []))
    return keys


def main() -> None:
    """Print one selected pred key per line."""
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--cached-only', action='store_true')
    parser.add_argument('--aws-profile', default=AWS_PROFILE)
    parser.add_argument('--region', default=REGION)
    args = parser.parse_args()

    session = boto3.Session(
        profile_name=args.aws_profile, region_name=args.region
    )
    s3 = session.client('s3')

    done: set[tuple[str, str]] = set()
    for key in list_keys(s3, TEST_RESULTS_S3_BUCKET_NAME):
        try:
            result = TestResultsFilename.decode(key)
        except ValueError:
            continue
        done.add((result.instance_id, str(result.patch_type)))

    # One key per (instance, patch type): the newest upload wins.
    latest: dict[tuple[str, str], tuple[S3PredFilename, str]] = {}
    for key in list_keys(s3, PREDS_S3_BUCKET_NAME):
        try:
            pred = S3PredFilename.decode(key)
        except ValueError:
            continue
        if benchmark_of(pred.instance_id) != Benchmark.SWE_BENCH_VERIFIED:
            continue
        if pred.patch_type not in PATCH_TYPES:
            continue
        slot = (pred.instance_id, str(pred.patch_type))
        if slot not in latest or pred.timestamp > latest[slot][0].timestamp:
            latest[slot] = (pred, key)

    ecr = session.client('ecr')
    cached: dict[str, bool] = {}
    selected = 0
    for slot, (pred, key) in sorted(latest.items()):
        if slot in done:
            continue
        if args.cached_only:
            if pred.instance_id not in cached:
                cached[pred.instance_id] = is_cached(
                    ecr, base_image(pred.instance_id)
                )
            if not cached[pred.instance_id]:
                continue
        print(key)
        selected += 1
    print(
        f'{selected} key(s) selected; {len(done & set(latest))} already done',
        file=sys.stderr,
    )


if __name__ == '__main__':
    main()
