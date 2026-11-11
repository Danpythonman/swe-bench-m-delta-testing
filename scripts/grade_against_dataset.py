"""Grade one agent run against the released labels in ``dataset/``.

The hosted API's worker runs this after ``run_instance.py`` has run the
agent's patch (``with_image --apply-test-patch --json --file``). It reads
that run's results from ``results/``, looks the instance up in
``dataset/<benchmark>_tests.parquet``, and scores it with the same code as
``derive_and_grade.py`` (:func:`sbmdt.analysis.reference.score_variant` and
:func:`sbmdt.analysis.reference.resolved`): the patch resolves the
instance when every FAIL_TO_PASS test passes and no PASS_TO_PASS test is
lost; a test that did not run counts against it.

Writes one JSON object (to ``--out``, or stdout):

    {"instance_id": ..., "resolved": true/false,
     "f2p": n, "f2p_passed": n, "p2p": n, "p2p_lost": n,
     "failed_f2p": [...], "lost_p2p": [...]}

Usage:
    uv run scripts/grade_against_dataset.py django__django-11099
    uv run scripts/grade_against_dataset.py django__django-11099 \\
        --results results --variant with_image --out verdict.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final

import pandas as pd

from sbmdt.analysis.reference import (
    INSTANCE,
    PASSED,
    TEST,
    disambiguate_test_names,
    resolved,
    score_variant,
)
from sbmdt.benchmark import Benchmark, benchmark_of
from sbmdt.env import PROJECT_BASE

DATASET: Final[Path] = PROJECT_BASE / 'dataset'
LISTED: Final[int] = 200
"""At most this many failing test names are listed per kind."""


def reference(instance: str, dataset: Path) -> pd.DataFrame:
    """The instance's FAIL_TO_PASS and PASS_TO_PASS tests."""
    name = (
        'verified'
        if benchmark_of(instance) == Benchmark.SWE_BENCH_VERIFIED
        else 'multimodal'
    )
    tests = pd.read_parquet(
        dataset / f'{name}_tests.parquet',
        filters=[(INSTANCE, '==', instance)],
    )
    tests = tests[tests['category'].isin(['FAIL_TO_PASS', 'PASS_TO_PASS'])]
    if tests.empty:
        raise SystemExit(f'{instance} has no labels in {dataset}')
    return tests.astype({INSTANCE: object, TEST: object, 'category': object})


def runs(instance: str, results: Path, variant: str) -> pd.DataFrame:
    """The agent run's per-test results, named as the dataset names them."""
    frames = [
        pd.DataFrame(json.loads(path.read_text()))
        for path in sorted(results.glob('*.json'))
    ]
    frames = [f for f in frames if not f.empty]
    if not frames:
        raise SystemExit(f'no results in {results}')
    frame = pd.concat(frames, ignore_index=True)
    frame = frame[
        (frame[INSTANCE] == instance) & (frame['patch_type'] == variant)
    ].copy()
    if frame.empty:
        raise SystemExit(f'no {variant} results for {instance}')
    frame[PASSED] = frame[PASSED].astype(bool)
    frame['timestamp'] = pd.to_datetime(frame['timestamp'], utc=True)
    if benchmark_of(instance) == Benchmark.SWE_BENCH_M:
        frame = disambiguate_test_names(frame)
    return frame


def grade(
    instance: str, results: Path, variant: str, dataset: Path
) -> dict[str, object]:
    scored = score_variant(
        runs(instance, results, variant),
        reference(instance, dataset),
        variant,
    )
    row = resolved(scored).iloc[0]
    f2p = scored[scored['category'] == 'FAIL_TO_PASS']
    p2p = scored[scored['category'] == 'PASS_TO_PASS']
    failed_f2p = sorted(f2p.loc[f2p['status'] != 'passed', TEST])
    lost_p2p = sorted(p2p.loc[p2p['status'] != 'passed', TEST])
    return {
        'instance_id': instance,
        'resolved': bool(row['resolved']),
        'f2p': int(row['f2p']),
        'f2p_passed': int(row['f2p_passed']),
        'p2p': int(row['p2p']),
        'p2p_lost': len(lost_p2p),
        'failed_f2p': failed_f2p[:LISTED],
        'lost_p2p': lost_p2p[:LISTED],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('instance', help='Benchmark instance id.')
    parser.add_argument(
        '--results',
        type=Path,
        default=PROJECT_BASE / 'results',
        help='Directory of run_instance.py --json --file results.',
    )
    parser.add_argument(
        '--variant',
        default='with_image',
        help='patch_type of the agent run (default: with_image).',
    )
    parser.add_argument('--dataset', type=Path, default=DATASET)
    parser.add_argument('--out', type=Path, help='Write the JSON here.')
    args = parser.parse_args()

    verdict = grade(args.instance, args.results, args.variant, args.dataset)
    text = json.dumps(verdict, indent=1)
    if args.out:
        args.out.write_text(text + '\n')
    else:
        sys.stdout.write(text + '\n')


if __name__ == '__main__':
    main()
