"""Split each instance's gold_patch.diff into its code and test halves.

SWE-bench evaluates a model patch by applying the model's *code* changes
and then applying the maintainer's *test* changes on top, so that the
FAIL_TO_PASS tests exist in the container regardless of what the model
wrote. This project's `gold_patch.diff` bundles both halves into a single
diff, and `Evaluator.apply_patch` only ever applies `pred.model_patch`.
A model run therefore never receives the maintainer's new tests, and its
FAIL_TO_PASS tests read as "not run" rather than as failures.

This script separates the two halves so the harness can apply them
independently. For every instance it writes:

    code_patch.diff       the non-test hunks of the gold patch
    test_patch.diff       the test-file hunks of the gold patch
    code_patch.pred       code_patch.diff wrapped as a Pred

`code_patch.pred` is the control experiment: it is the maintainer's own
fix with the tests stripped out, which is exactly the shape of a model
submission. Run it through the harness as a model patch. If it scores
zero on FAIL_TO_PASS, the harness is at fault rather than the model,
because the known-correct answer just failed.

Usage:
    uv run scripts/split_gold_patch.py                    # every instance
    uv run scripts/split_gold_patch.py --instance <id>    # just one
    uv run scripts/split_gold_patch.py --report           # summarise only
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Final

from sbmdt.env import DOCKERFILES_BASE
from sbmdt.log import setup_logging
from sbmdt.patches import (
    CODE_PATCH_DIFF_FILENAME,
    CODE_PATCH_PRED_FILENAME,
    DIFF_HEADER,
    GOLD_PATCH_DIFF_FILENAME,
    TEST_PATCH_DIFF_FILENAME,
    read_diff,
    split_diff,
)
from sbmdt.pred import Pred

log = logging.getLogger(__name__)

CODE_MODEL_NAME: Final[str] = 'GOLD_CODE_ONLY'


def split_instance(instance_dir: Path, write: bool = True) -> dict[str, int]:
    """Split one instance's gold patch and write the three artifacts.

    Args:
        instance_dir: The instance's directory under `dockerfiles/`.
        write: When False, compute the split without writing anything.

    Returns:
        Counts of the code and test files found, keyed `code` and `test`.

    Raises:
        FileNotFoundError: If the instance has no gold_patch.diff.
    """
    gold = instance_dir / GOLD_PATCH_DIFF_FILENAME
    if not gold.is_file():
        raise FileNotFoundError(f'no gold patch: {gold}')

    diff = read_diff(gold)
    code_diff, test_diff = split_diff(diff)

    counts = {
        'code': len(DIFF_HEADER.findall(code_diff)),
        'test': len(DIFF_HEADER.findall(test_diff)),
    }
    if not write:
        return counts

    (instance_dir / CODE_PATCH_DIFF_FILENAME).write_text(
        code_diff, encoding='utf-8', errors='surrogateescape'
    )
    (instance_dir / TEST_PATCH_DIFF_FILENAME).write_text(
        test_diff, encoding='utf-8', errors='surrogateescape'
    )

    pred = Pred(
        instance_id=instance_dir.name,
        model_name_or_path=CODE_MODEL_NAME,
        model_patch=code_diff,
    )
    (instance_dir / CODE_PATCH_PRED_FILENAME).write_text(
        json.dumps(asdict(pred)), encoding='utf-8'
    )
    return counts


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(
        description="Split gold_patch.diff into code and test halves."
    )
    parser.add_argument(
        '--instance',
        default=None,
        help='Only split this instance id. Defaults to every instance.',
    )
    parser.add_argument(
        '--dockerfiles',
        type=Path,
        default=DOCKERFILES_BASE,
        help='Directory of per-instance folders.',
    )
    parser.add_argument(
        '--report',
        action='store_true',
        help='Summarise the split without writing any files.',
    )
    return parser.parse_args()


def main() -> None:
    """Split every requested instance and report what was produced."""
    args = parse_args()
    setup_logging(level=logging.INFO)

    if args.instance:
        dirs = [args.dockerfiles / args.instance]
    else:
        dirs = sorted(p for p in args.dockerfiles.iterdir() if p.is_dir())

    both = code_only = test_only = neither = 0
    for instance_dir in dirs:
        try:
            counts = split_instance(instance_dir, write=not args.report)
        except FileNotFoundError:
            log.warning(f'{instance_dir.name}: no gold patch, skipped')
            continue
        if counts['code'] and counts['test']:
            both += 1
        elif counts['code']:
            code_only += 1
        elif counts['test']:
            test_only += 1
        else:
            neither += 1
        if args.instance:
            log.info(
                f'{instance_dir.name}: '
                f'{counts["code"]} code file(s), {counts["test"]} test file(s)'
            )

    verb = 'would split' if args.report else 'split'
    log.info(f'{verb} {len(dirs)} instance(s)')
    log.info(f'  both code and tests : {both}')
    log.info(f'  code changes only   : {code_only}')
    log.info(f'  test changes only   : {test_only}')
    log.info(f'  neither             : {neither}')
    if not args.report:
        log.info(
            f'wrote {CODE_PATCH_DIFF_FILENAME}, {TEST_PATCH_DIFF_FILENAME} '
            f'and {CODE_PATCH_PRED_FILENAME} per instance'
        )


if __name__ == '__main__':
    main()
