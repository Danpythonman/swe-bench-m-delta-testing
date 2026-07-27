"""Wrap each instance's gold_patch.diff into a gold_patch.pred prediction file.

Walks the local dockerfiles directory, and for every instance that already
has a `gold_patch.diff` (see `scripts/get_gold_patches.py`) but no
`gold_patch.pred` yet, wraps the diff in a `Pred` (with `model_name_or_path`
set to `'GOLD'`) and writes it out as JSON. This lets the gold patch be fed
through the same prediction-consuming pipeline as a real model's output.

Run it directly as a script: `uv run format_gold_diff_into_pred.py` or
`python format_gold_diff_into_pred.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Final

import aiofiles

from sbmdt.env import DOCKERFILES_BASE
from sbmdt.gold import (
    GOLD_MODEL_NAME,
    GOLD_PATCH_DIFF_FILENAME,
    GOLD_PATCH_PRED_FILENAME,
)
from sbmdt.log import setup_logging
from sbmdt.pred import Pred

log = logging.getLogger(__name__)

MAX_CONCURRENCY: Final[int] = 10


async def process(filepath: Path, sem: asyncio.Semaphore) -> None:
    """Read one instance's gold diff and write it out as a gold_patch.pred.

    The result lands at `filepath/gold_patch.pred`, a JSON-encoded `Pred`
    with `model_name_or_path='GOLD'` and `model_patch` set to the diff
    contents read from `filepath/gold_patch.diff`.
    """
    diff_filepath = filepath / GOLD_PATCH_DIFF_FILENAME
    pred_filepath = filepath / GOLD_PATCH_PRED_FILENAME
    async with sem:
        async with aiofiles.open(diff_filepath) as f_in:
            diff = await f_in.read()
            pred = Pred(
                instance_id=filepath.name,
                model_name_or_path=GOLD_MODEL_NAME,
                model_patch=diff,
            )
            pred_dict = asdict(pred)
            pred_json = json.dumps(pred_dict)
            async with aiofiles.open(pred_filepath, 'w') as f_out:
                await f_out.write(pred_json)


async def main():
    """Find every instance with a gold diff but no pred, and convert them all.

    Scans `DOCKERFILES_BASE` for instance folders that have a
    `gold_patch.diff` but not yet a `gold_patch.pred`, then converts them
    concurrently. Folders missing the diff entirely are skipped.
    """
    setup_logging()

    sem = asyncio.Semaphore()

    paths: list[Path] = []
    for path in DOCKERFILES_BASE.glob('*'):
        gold_pred_filepath = path / GOLD_PATCH_PRED_FILENAME
        if gold_pred_filepath.exists():
            continue
        gold_diff_filepath = path / GOLD_PATCH_DIFF_FILENAME
        if not gold_diff_filepath.exists():
            log.warning(f'{path.name} does not have a gold diff')
            continue
        paths.append(path)
    tasks = [asyncio.create_task(process(path, sem)) for path in paths]
    await asyncio.gather(*tasks)


if __name__ == '__main__':
    asyncio.run(main())
