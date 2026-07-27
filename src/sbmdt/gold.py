"""Helpers for fetching SWE-bench gold patches from GitHub.

Provides the pieces `scripts/get_gold_patches.py` uses to turn a SWE-bench
instance ID into a downloaded `gold_patch.diff`: parsing/rendering instance
IDs (`SWEBenchInstance`), filename constants for the gold patch artifacts,
and a rate-limit-aware `fetch` for hitting the GitHub API without tripping
its 403/429 throttling (handled via `RateLimited` and exponential backoff).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Final

import aiohttp
from aiolimiter import AsyncLimiter

__all__ = [
    'GOLD_PATCH_DIFF_FILENAME',
    'GOLD_PATCH_PRED_FILENAME',
    'MAX_CONCURRENCY',
    'MAX_UNAUTHENTICATED_GITHUB_REQUESTS_PER_HOUR',
    'MAX_AUTHENTICATED_GITHUB_REQUESTS_PER_HOUR',
    'SWEBenchInstance',
    'hours_to_seconds',
    'RateLimitedException',
    'fetch',
]

log = logging.getLogger(__name__)

GOLD_PATCH_DIFF_FILENAME: Final[str] = 'gold_patch.diff'
GOLD_PATCH_PRED_FILENAME: Final[str] = 'gold_patch.pred'

MAX_CONCURRENCY: Final[int] = 5

MAX_UNAUTHENTICATED_GITHUB_REQUESTS_PER_HOUR: Final[int] = 60
MAX_AUTHENTICATED_GITHUB_REQUESTS_PER_HOUR: Final[int] = 5_000

GOLD_MODEL_NAME: Final[str] = 'GOLD'


def hours_to_seconds(hours: int | float) -> int | float:
    """Convert a number of hours into seconds.

    Tiny helper so that rate limiter configs read as "X requests per hour"
    instead of a mystery number of seconds.
    """
    return hours * 60 * 60


@dataclass(kw_only=True)
class SWEBenchInstance:
    """A single SWE-bench instance, identified by its GitHub PR.

    SWE-bench instance IDs encode the owner, repo, and PR number as a
    string like `owner__repo-1234`. This class is the bridge between that
    string form and the pieces needed to actually go fetch the PR.
    """

    owner: str
    repo: str
    pr_number: int

    def to_github_pr_url(self) -> str:
        """Build the GitHub API URL for this instance's pull request."""
        return (
            f'https://api.github.com/repos/'
            f'{self.owner}/{self.repo}/pulls/{self.pr_number}'
        )

    def to_instance_id(self) -> str:
        """Render this instance back into the standard `owner__repo-N` ID."""
        return f'{self.owner}__{self.repo}-{self.pr_number}'

    @staticmethod
    def parse_instance_id(instance_id: str) -> SWEBenchInstance:
        """Parse an `owner__repo-N` instance ID into its parts.

        Splits on the first `__` for the owner, then the last `-` for the
        PR number, since repo names themselves can contain dashes (looking
        at you, `owner__repo-with-dashes-1234`).

        Raises ValueError if the ID doesn't match the expected shape.
        """
        owner, sep, rest = instance_id.partition('__')
        if not sep or not rest:
            raise ValueError(f'Invalid instance_id: {instance_id!r}')
        repo, sep, number = rest.rpartition('-')
        if not sep or not repo:
            raise ValueError(f'Invalid instance_id: {instance_id!r}')
        return SWEBenchInstance(owner=owner, repo=repo, pr_number=int(number))


class RateLimitedException(Exception):
    """Raised internally when GitHub answers with a 403 or 429.

    Carries the number of seconds the caller should wait before trying
    again, as worked out from the response headers.
    """

    def __init__(self, delay: float) -> None:
        self.delay = delay


def _retry_delay(response: aiohttp.ClientResponse, attempt: int) -> float:
    """Work out how long to wait before retrying a rate-limited request.

    Prefers GitHub's explicit `Retry-After` header. Failing that, checks
    whether the rate limit window itself is exhausted via
    `X-RateLimit-Remaining`/`X-RateLimit-Reset` and waits until it resets.
    If GitHub gives no hints at all, falls back to exponential backoff
    based on the attempt number.
    """
    retry_after = response.headers.get('Retry-After')
    if retry_after:
        return float(retry_after)
    reset = response.headers.get('X-RateLimit-Reset')
    remaining = response.headers.get('X-RateLimit-Remaining')
    if remaining == '0' and reset:
        reset_time_seconds = float(reset)
        current_time_seconds = time.time()
        return max(reset_time_seconds - current_time_seconds, 0) + 1
    # At least 60 seconds
    return 60 * 2**attempt + random.randint(0, 60)


async def _fetch_once(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str] | None,
    sem: asyncio.Semaphore,
    limiter: AsyncLimiter,
    attempt: int,
) -> str:
    """Make a single GET request, guarded by the limiter and semaphore.

    Raises RateLimited if GitHub responds with 403 or 429, so the caller
    can sleep and retry. Any other non-2xx status raises the usual
    aiohttp error. Returns the response body as text on success.
    """
    async with limiter:
        async with sem:
            log.debug(f'Hitting {url}')
            async with session.get(url, headers=headers) as response:
                if response.status in (403, 429):
                    raise RateLimitedException(_retry_delay(response, attempt))
                # Raise exception for all other errors
                response.raise_for_status()
                return await response.text()


async def fetch(
    session: aiohttp.ClientSession,
    url: str,
    github_token: str | None,
    sem: asyncio.Semaphore,
    limiter: AsyncLimiter,
    max_retries: int = 5,
) -> str:
    """Fetch a URL, automatically retrying on GitHub rate limit responses.

    Wraps `_fetch_once` in a retry loop: each time a :class:`RateLimited`
    error comes back, it logs a warning, sleeps for the suggested delay, and
    tries again, up to :param:`max_retries` attempts. Raises RuntimeError if it
    never succeeds.
    """
    headers = (
        {'Authorization': f'Bearer {github_token}'} if github_token else None
    )

    for attempt in range(max_retries):
        try:
            return await _fetch_once(
                session, url, headers, sem, limiter, attempt
            )
        except RateLimitedException as e:
            log.warning(f'Rate limited on {url}, sleeping {e.delay} seconds')
            await asyncio.sleep(e.delay)

    raise RuntimeError(f'Max retries exceeded for {url}')
