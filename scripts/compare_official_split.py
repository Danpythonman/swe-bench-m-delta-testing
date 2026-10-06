"""Compare this harness's test split with SWE-bench Verified's labels.

The harness derives FAIL_TO_PASS and PASS_TO_PASS on its own, from
``before_patch`` and ``gold`` runs, with the same classifier used for
SWE-bench M (:mod:`sbmdt.analysis.test_split`). The published lists in each
instance's ``reference.json`` are read here and nowhere else, so this
comparison is an independent check of the harness rather than a
restatement of the benchmark.

Agreement is reported per instance and per repository. Every official
test the harness disagrees with is explained by what the harness actually
observed for it:

``pass_to_pass`` / ``fail_to_pass``
    The harness ran the test and classified it the other way. This is a
    genuine disagreement: either the harness environment differs or the
    official label is wrong (both happen).
``broken`` / ``regressed`` / ``flaky``
    The harness ran it but saw it fail after the patch, or inconsistently.
``not_run``
    The test never produced a result: it was not selected, its module
    failed to import, or its name is spelled differently.
``ambiguous``
    Several harness tests match the official name (SymPy labels are bare
    function names, pytest labels are truncated at the first space,
    Django labels can be docstring lines) and the harness classified
    them differently.

Usage:
    uv run scripts/compare_official_split.py               # synced parquet
    uv run scripts/compare_official_split.py --data results
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from sbmdt.analysis.reference import (
    INSTANCE,
    POST,
    PRE,
    reference_split,
    render,
)
from sbmdt.analysis.verified import (
    OFFICIAL,
    compare,
    extra_fail_to_pass,
    load_verified_results,
    per_instance,
    per_repo,
    with_keys,
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
        description='Compare the derived split with SWE-bench Verified.'
    )
    parser.add_argument(
        '--data',
        type=Path,
        default=PROJECT_BASE / 's3-sync' / 'test-results',
        help='Directory of Parquet or JSON results.',
    )
    parser.add_argument(
        '--out',
        type=Path,
        default=PROJECT_BASE / 'analysis' / 'verified',
        help='Directory to write CSVs to.',
    )
    return parser.parse_args()


def main() -> None:
    """Compare and write the per-test, per-instance and per-repo CSVs."""
    args = parse_args()
    setup_logging(level=logging.INFO)

    frame = load_verified_results(args.data)
    log.info(f'{len(frame):,} rows over {frame[INSTANCE].nunique()} instances')
    tests, _ = reference_split(frame, PRE, POST)
    tests = with_keys(tests)

    official = compare(tests)
    extra = extra_fail_to_pass(tests, official)
    summary = per_instance(official, extra)
    table = per_repo(summary)

    print()
    print(render(table))
    print()
    disagreements = official[
        official['observed'] != official['label'].map(OFFICIAL)
    ]
    print('Official tests the harness disagrees with, by what it observed:')
    print(
        disagreements.groupby(['label', 'observed']).size().to_string()
        if not disagreements.empty
        else '  none'
    )

    args.out.mkdir(parents=True, exist_ok=True)
    official.to_csv(args.out / 'official_tests.csv', index=False)
    disagreements.to_csv(args.out / 'disagreements.csv', index=False)
    extra.to_csv(args.out / 'extra_fail_to_pass.csv', index=False)
    summary.to_csv(args.out / 'by_instance.csv', index=False)
    table.to_csv(args.out / 'by_repo.csv')
    log.info(f'wrote CSVs to {args.out}')


if __name__ == '__main__':
    main()
