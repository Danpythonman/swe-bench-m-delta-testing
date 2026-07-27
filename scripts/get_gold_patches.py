"""Backfill gold patches for SWE-bench instances by scraping GitHub PR diffs.

Walks the local dockerfiles directory, finds every instance that doesn't
already have a `gold_patch.diff` file, and fetches the corresponding pull
request diff from the GitHub API. Requests are throttled to stay under
GitHub's rate limits and retried with backoff when a 403/429 shows up.

Run it directly as a script: `uv run get_gold_patches.py` or
`python get_gold_patches.py`. Set GITHUB_TOKEN in the environment to get the
much friendlier authenticated rate limit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import aiofiles
import aiohttp
from aiolimiter import AsyncLimiter

from sbmdt.env import DOCKERFILES_BASE
from sbmdt.gold import (
    GOLD_PATCH_DIFF_FILENAME,
    MAX_AUTHENTICATED_GITHUB_REQUESTS_PER_HOUR,
    MAX_CONCURRENCY,
    MAX_UNAUTHENTICATED_GITHUB_REQUESTS_PER_HOUR,
    SWEBenchInstance,
    fetch,
    hours_to_seconds,
)
from sbmdt.log import setup_logging

log = logging.getLogger(__name__)


async def process(
    session: aiohttp.ClientSession,
    instance: SWEBenchInstance,
    github_token: str | None,
    sem: asyncio.Semaphore,
    limiter: AsyncLimiter,
) -> None:
    """Fetch one instance's PR diff and write it out as its gold patch.

    The result lands at `DOCKERFILES_BASE/<instance_id>/gold_patch.diff`,
    matching the layout the rest of the pipeline expects.
    """
    log.info(f'Processing {instance.to_instance_id()}')
    log.debug(f'{instance.to_instance_id()}: Getting PR metadata')
    pr_info = await fetch(
        session, instance.to_github_pr_url(), github_token, sem, limiter
    )
    pr_info_json = json.loads(pr_info)
    diff_url = pr_info_json.get('diff_url')
    if diff_url:
        log.debug(f'{instance.to_instance_id()}: Getting PR patch')
        patch = await fetch(session, str(diff_url), github_token, sem, limiter)
        gold_pred_filepath = (
            DOCKERFILES_BASE
            / instance.to_instance_id()
            / GOLD_PATCH_DIFF_FILENAME
        )
        log.debug(f'{instance.to_instance_id()} Writing patch to file')
        async with aiofiles.open(gold_pred_filepath, 'w') as f:
            await f.write(patch)
        log.info(f'Processing successful for {instance.to_instance_id()}')
    else:
        log.warning(f'{instance.to_instance_id()} has no diff')


async def main():
    """Find every instance missing a gold patch and fetch them all.

    Scans `DOCKERFILES_BASE` for instance folders that don't yet have a
    `gold_patch.diff`, picks an appropriate rate limiter depending on
    whether a `GITHUB_TOKEN` is set, and downloads the missing patches
    concurrently (bounded by `MAX_CONCURRENCY`).
    """
    setup_logging()
    instances_to_fetch: list[SWEBenchInstance] = []
    for dockerfile_folder in DOCKERFILES_BASE.glob('*'):
        if not dockerfile_folder.is_dir():
            continue
        gold_pred_filepath = dockerfile_folder / GOLD_PATCH_DIFF_FILENAME
        if gold_pred_filepath.exists():
            continue
        instance_id = dockerfile_folder.name
        instance = SWEBenchInstance.parse_instance_id(instance_id)
        instances_to_fetch.append(instance)

    github_token = os.environ.get('GITHUB_TOKEN', None)
    if not github_token:
        limiter = AsyncLimiter(
            MAX_UNAUTHENTICATED_GITHUB_REQUESTS_PER_HOUR, hours_to_seconds(1)
        )
        log.warning('GitHub token not provided, going unauthenticated')
    else:
        limiter = AsyncLimiter(
            MAX_AUTHENTICATED_GITHUB_REQUESTS_PER_HOUR, hours_to_seconds(1)
        )
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    async with aiohttp.ClientSession() as session:
        tasks = [
            asyncio.create_task(
                process(session, instance, github_token, sem, limiter)
            )
            for instance in instances_to_fetch
        ]
        await asyncio.gather(*tasks)


if __name__ == '__main__':
    asyncio.run(main())
