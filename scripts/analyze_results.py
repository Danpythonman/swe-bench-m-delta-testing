"""Summarise the synced test results.

Two questions, one per subcommand.

``reference`` asks what the maintainers' patches did to the test suites.
It classifies every test in every instance that has runs on both sides of
its reference patch (``before_patch`` against ``gold``) and prints the
per-repository table.

``score`` asks how a model patch fared against that reference split. It
takes the FAIL_TO_PASS and PASS_TO_PASS sets ``reference`` produces and
looks each of those tests up in the model-patch runs, which is what
SWE-bench actually measures. Note that a model run only contains the
maintainer's tests when the evaluation was run with
``--apply-test-patch`` (see ``scripts/split_gold_patch.py``); without it
every FAIL_TO_PASS test reads as ``not_run`` and no patch can score.

The analysis lives in :mod:`sbmdt.analysis.reference` (and the
classifier in :mod:`sbmdt.analysis.test_split`), so notebooks and other
scripts import it from there; this file is only the command line.

Usage:
    uv run scripts/analyze_results.py reference
    uv run scripts/analyze_results.py reference --self-check
    uv run scripts/analyze_results.py score --variant without_image
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sbmdt.analysis.reference import (
    INSTANCE,
    POST,
    PRE,
    VARIANTS,
    check,
    load_results,
    reference_split,
    reference_table,
    render,
    resolved,
    score_table,
    score_variant,
)
from sbmdt.env import PROJECT_BASE
from sbmdt.log import setup_logging

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(
        description='Summarise the synced test results.'
    )
    parser.add_argument(
        '--data',
        type=Path,
        default=PROJECT_BASE / 's3-sync' / 'test-results',
        help='Directory of synced Parquet results.',
    )
    parser.add_argument(
        '--out',
        type=Path,
        default=PROJECT_BASE / 'analysis',
        help='Directory to write CSVs to.',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    ref = sub.add_parser(
        'reference', help='Classify before_patch against gold.'
    )
    ref.add_argument(
        '--self-check',
        action='store_true',
        help='Compare the result against the published report and exit '
        'non-zero on any mismatch.',
    )

    sco = sub.add_parser(
        'score', help='Score model patches against the reference split.'
    )
    sco.add_argument(
        '--variant',
        choices=[*VARIANTS, 'all'],
        default='all',
        help='Which model prediction set to score.',
    )
    return parser.parse_args()


def main() -> None:
    """Run the requested analysis and write its CSVs."""
    args = parse_args()
    setup_logging(level=logging.INFO)

    frame = load_results(args.data)
    log.info(f'{len(frame):,} rows over {frame[INSTANCE].nunique()} instances')

    tests, _ = reference_split(frame, PRE, POST)
    args.out.mkdir(parents=True, exist_ok=True)

    if args.command == 'reference':
        table = reference_table(tests)
        print()
        print(render(table))
        print()
        tests.to_csv(args.out / 'reference_tests.csv', index=False)
        table.to_csv(args.out / 'reference_by_repo.csv')
        log.info(
            'wrote reference_tests.csv and reference_by_repo.csv to '
            f'{args.out}'
        )
        if args.self_check:
            log.info('checking against the published report:')
            sys.exit(check(tests))
        return

    variants = VARIANTS if args.variant == 'all' else (args.variant,)
    for variant in variants:
        scored = score_variant(frame, tests, variant)
        if scored.empty:
            log.warning(f'{variant}: no scorable instance, skipped')
            continue
        summary = resolved(scored)
        print()
        print(f'=== {variant} ===')
        print(render(score_table(scored)))
        print()
        f2p = scored[scored['category'] == 'FAIL_TO_PASS']
        log.info(
            f'{variant}: {summary[INSTANCE].nunique()} instance(s) scored, '
            f'{int((f2p["status"] == "passed").sum())} of {len(f2p)} '
            f'FAIL_TO_PASS passed, '
            f'{int(summary["resolved"].sum())} resolved'
        )
        if int((f2p['status'] == 'not_run').sum()) == len(f2p) and len(f2p):
            log.warning(
                f'{variant}: every FAIL_TO_PASS test is not_run, which means '
                'these runs were evaluated without the gold test patch. See '
                'scripts/split_gold_patch.py and --apply-test-patch.'
            )
        scored.to_csv(args.out / f'scored_{variant}.csv', index=False)
        summary.to_csv(args.out / f'resolved_{variant}.csv', index=False)
        log.info(
            f'wrote scored_{variant}.csv and resolved_{variant}.csv '
            f'to {args.out}'
        )


if __name__ == '__main__':
    main()
