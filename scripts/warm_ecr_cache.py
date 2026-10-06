"""Populate the ECR pull-through cache with every Verified base image.

The ECR cache fetches an image from Docker Hub the first time anyone asks
for it, and Docker Hub rate-limits those fetches. A large EC2 batch asks
for hundreds of uncached images within minutes, so workers fail their
image build with ``toomanyrequests`` while the quota recovers. Warming
the cache beforehand moves every upstream fetch here, one at a time, where
throttling just means waiting; the workers then only ever hit the cache.

Requesting an image's manifest is enough: ECR creates the cached
repository and imports the whole image server side, so nothing is
downloaded locally.

Usage:
    uv run scripts/warm_ecr_cache.py
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import time
from typing import Final

import boto3

from sbmdt.aws.ecr import base_image, docker_login, is_cached
from sbmdt.aws.env import AWS_PROFILE, REGION
from sbmdt.benchmark import Benchmark, benchmark_of
from sbmdt.env import DOCKERFILES_BASE
from sbmdt.log import setup_logging

log = logging.getLogger(__name__)

THROTTLED_DELAY_SECONDS: Final[float] = 300.0
# Docker Hub's per-account window can stay closed for hours, so waiting
# is the only way through; give up on an image only after a full day.
MAX_ATTEMPTS: Final[int] = 24 * 3600 // int(THROTTLED_DELAY_SECONDS)


def warm(image: str) -> None:
    """Request ``image``'s manifest, waiting out Docker Hub throttling.

    Args:
        image: A full image reference in the ECR registry.

    Raises:
        RuntimeError: If the request still fails after every attempt.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = subprocess.run(
            ['docker', 'manifest', 'inspect', image],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        error = result.stderr.strip()
        if 'toomanyrequests' not in error and 'rate' not in error.lower():
            raise RuntimeError(f'{image}: {error}')
        log.warning(
            f'Docker Hub throttled (attempt {attempt}/{MAX_ATTEMPTS}); '
            f'waiting {THROTTLED_DELAY_SECONDS:.0f}s'
        )
        time.sleep(THROTTLED_DELAY_SECONDS)
    raise RuntimeError(f'{image}: still throttled after {MAX_ATTEMPTS} tries')


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(
        description='Warm the ECR pull-through cache for Verified images.'
    )
    parser.add_argument('--aws-profile', default=AWS_PROFILE)
    parser.add_argument('--region', default=REGION)
    return parser.parse_args()


def main() -> None:
    """Warm every uncached Verified base image."""
    args = parse_args()
    setup_logging(level=logging.INFO)

    session = boto3.Session(
        profile_name=args.aws_profile, region_name=args.region
    )
    ecr = session.client('ecr')
    images = sorted(
        {
            base_image(path.name)
            for path in DOCKERFILES_BASE.iterdir()
            if benchmark_of(path.name) == Benchmark.SWE_BENCH_VERIFIED
        }
    )
    missing = [image for image in images if not is_cached(ecr, image)]
    log.info(f'{len(images)} images, {len(missing)} not cached yet')
    if not missing:
        return

    docker_login(session, missing[0].split('/', 1)[0])
    failed: list[str] = []
    for number, image in enumerate(missing, start=1):
        try:
            warm(image)
        except RuntimeError as exc:
            log.error(str(exc))
            failed.append(image)
            continue
        log.info(f'[{number}/{len(missing)}] warmed {image.rsplit("/", 1)[1]}')
    log.info(
        f'done: {len(missing) - len(failed)} warmed, {len(failed)} failed'
    )
    for image in failed:
        log.error(f'not warmed: {image}')


if __name__ == '__main__':
    main()
