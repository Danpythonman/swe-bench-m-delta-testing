"""Derive each instance's test split from local runs, and grade agent patches.

The last step of a local walkthrough. ``run_instance.py --file`` writes
one results file per run into ``results/``; this script reads them all
(JSON or Parquet, SWE-bench Verified or Multimodal) and answers the two
questions a user has after running them:

1. **What are this instance's FAIL_TO_PASS and PASS_TO_PASS tests?**
   Every instance with a ``before_patch`` and a ``gold`` run is
   classified by :func:`sbmdt.analysis.reference.reference_split`, the
   same code the paper's numbers come from.
2. **Did an agent's patch resolve it?** Every ``with_image`` or
   ``without_image`` run is looked up against that split, per agent.
   Resolved means every FAIL_TO_PASS test passed and every PASS_TO_PASS
   test still passed; a test that did not run counts as failed. Run the
   agent patch with ``--apply-test-patch``, or its FAIL_TO_PASS tests
   don't exist in the container and can never pass.

It writes the split as ``split.json`` (instance -> category -> test
names) and the verdicts as ``verdicts.csv``, and prints a summary.

``scripts/analyze_results.py`` gives the per-repository view of a full
Multimodal sync, and ``scripts/compare_official_split.py`` compares a
Verified split with the official labels; this script needs neither.

Usage:
    uv run scripts/derive_and_grade.py
    uv run scripts/derive_and_grade.py --instance django__django-11099 --list
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Final

import pandas as pd

from sbmdt.analysis.reference import (
    INSTANCE,
    PASSED,
    PATCH,
    POST,
    PRE,
    REPO,
    TEST,
    VARIANTS,
    disambiguate_test_names,
    reference_split,
    repo_of,
    resolved,
    score_variant,
)
from sbmdt.benchmark import Benchmark, benchmark_of
from sbmdt.env import PROJECT_BASE
from sbmdt.log import setup_logging
from sbmdt.parquet import read_test_results

log = logging.getLogger(__name__)

CATEGORIES: Final[tuple[str, ...]] = (
    'FAIL_TO_PASS',
    'PASS_TO_PASS',
    'REGRESSED',
    'BROKEN',
    'FLAKY',
)
SHORT: Final[dict[str, str]] = {
    'FAIL_TO_PASS': 'F2P',
    'PASS_TO_PASS': 'P2P',
    'REGRESSED': 'regressed',
    'BROKEN': 'broken',
    'FLAKY': 'flaky',
}


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(
        description='Derive the test split from local runs and grade '
        'agent patches against it.'
    )
    parser.add_argument(
        '--data',
        type=Path,
        default=PROJECT_BASE / 'results',
        help='Directory of JSON or Parquet results (default: results/, '
        'where run_instance.py --file writes).',
    )
    parser.add_argument(
        '--out',
        type=Path,
        default=PROJECT_BASE / 'analysis' / 'derived',
        help='Directory to write split.json and verdicts.csv to.',
    )
    parser.add_argument(
        '--instance',
        action='append',
        help='Only this instance (repeatable).',
    )
    parser.add_argument(
        '--list',
        action='store_true',
        help='Also print every FAIL_TO_PASS test, and every F2P or P2P '
        'test an agent patch did not pass.',
    )
    return parser.parse_args()


def load(data_dir: Path, instances: list[str] | None) -> pd.DataFrame:
    """Read every JSON and Parquet result in a directory.

    Args:
        data_dir: Directory of result files.
        instances: Instance ids to keep, or ``None`` for all.

    Returns:
        One row per test result, with test names that several files
        define qualified as ``file::name`` (Multimodal only, as in
        :func:`sbmdt.analysis.reference.load_results`).

    Raises:
        SystemExit: If the directory holds no matching results.
    """
    if not data_dir.is_dir():
        raise SystemExit(f'no such directory: {data_dir}')
    # run_instance.py --json writes *.json; Parquet objects synced from S3
    # carry no extension, so every other file is read as Parquet.
    files = sorted(
        p
        for p in data_dir.iterdir()
        if p.is_file() and not p.name.startswith('.')
    )
    frames = [
        pd.DataFrame(json.loads(p.read_text()))
        if p.suffix == '.json'
        else read_test_results(p)
        for p in files
    ]
    frames = [f for f in frames if not f.empty]
    if not frames:
        raise SystemExit(f'no results in {data_dir}')
    frame = pd.concat(frames, ignore_index=True)
    if instances:
        frame = frame[frame[INSTANCE].isin(instances)]
        if frame.empty:
            raise SystemExit(f'no results for {", ".join(instances)}')
    frame[PASSED] = frame[PASSED].astype(bool)
    frame['timestamp'] = pd.to_datetime(frame['timestamp'], utc=True)
    frame[REPO] = frame[INSTANCE].map(repo_of)
    is_m = frame[INSTANCE].map(benchmark_of) == Benchmark.SWE_BENCH_M
    return pd.concat(
        [disambiguate_test_names(frame[is_m]), frame[~is_m]],
        ignore_index=True,
    )


def split_json(tests: pd.DataFrame) -> dict[str, dict[str, list[str]]]:
    """Group the classified tests by instance and category.

    Args:
        tests: The per-test frame from ``reference_split``.

    Returns:
        instance -> category -> sorted test names.
    """
    out: dict[str, dict[str, list[str]]] = {}
    for (instance, category), group in tests.groupby(
        [INSTANCE, 'category'], observed=True
    ):
        out.setdefault(instance, {})[category] = sorted(group[TEST])
    return {k: out[k] for k in sorted(out)}


def print_split(split: dict[str, dict[str, list[str]]], names: bool) -> None:
    """Print one line of counts per instance, and optionally F2P names.

    Args:
        split: The output of :func:`split_json`.
        names: Whether to list every FAIL_TO_PASS test.
    """
    print('Derived split (before_patch vs gold):')
    width = max(len(i) for i in split)
    for instance, cats in split.items():
        counts = ', '.join(
            f'{len(cats.get(c, []))} {SHORT[c]}'
            for c in CATEGORIES
            if c in ('FAIL_TO_PASS', 'PASS_TO_PASS') or cats.get(c)
        )
        print(f'  {instance:<{width}}  {counts}')
        if names:
            for test in cats.get('FAIL_TO_PASS', []):
                print(f'      F2P  {test}')


def grade(
    frame: pd.DataFrame, tests: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Grade every agent's runs against the derived split.

    Args:
        frame: Every result row.
        tests: The per-test frame from ``reference_split``.

    Returns:
        A ``(verdicts, failures)`` pair. ``verdicts`` has one row per
        (instance, variant, agent), with counts and a ``resolved`` flag;
        ``failures`` has one row per F2P or P2P test that did not pass
        (failed, not run, or flaky). Both are empty if there are no
        agent runs.
    """
    runs = frame[frame[PATCH].isin(VARIANTS)]
    verdicts = []
    failures = []
    for (variant, agent), _ in runs.groupby(
        [PATCH, 'agent_name'], observed=True
    ):
        scored = score_variant(frame, tests, variant, agent=agent)
        if scored.empty:
            continue
        summary = resolved(scored)
        summary.insert(1, 'agent_name', agent)
        verdicts.append(summary)
        failed = scored[scored['status'] != 'passed'].copy()
        failed['agent_name'] = agent
        failures.append(failed)
    graded = set(pd.concat(verdicts)[INSTANCE]) if verdicts else set()
    missing = sorted(set(runs[INSTANCE]) - graded)
    if missing:
        log.warning(
            'agent runs without a before_patch and gold run to grade '
            f'against: {", ".join(missing)}'
        )
    if not verdicts:
        return pd.DataFrame(), pd.DataFrame()
    return (
        pd.concat(verdicts, ignore_index=True),
        pd.concat(failures, ignore_index=True),
    )


def print_verdicts(
    verdicts: pd.DataFrame, failures: pd.DataFrame, names: bool
) -> None:
    """Print one line per graded agent run.

    Args:
        verdicts: The verdicts from :func:`grade`.
        failures: The tests that did not pass, from :func:`grade`.
        names: Whether to list the tests that did not pass.
    """
    print('Agent patches:')
    width = verdicts[INSTANCE].str.len().max()
    for r in verdicts.itertuples(index=False):
        verdict = 'RESOLVED' if r.resolved else 'UNRESOLVED'
        detail = f'F2P {r.f2p_passed}/{r.f2p} passed'
        if r.f2p_not_run:
            detail += f' ({r.f2p_not_run} not run)'
        detail += f', P2P {r.p2p - r.p2p_passed} of {r.p2p} lost'
        print(
            f'  {r.instance_id:<{width}}  {r.agent_name} ({r.variant}): '
            f'{verdict:<10}  {detail}'
        )
        if names:
            mine = failures[
                (failures[INSTANCE] == r.instance_id)
                & (failures['agent_name'] == r.agent_name)
                & (failures['variant'] == r.variant)
            ]
            for f in mine.sort_values(['category', TEST]).itertuples():
                label = SHORT[f.category]
                print(f'      {label} {f.status:<7}  {f.test_name}')
    if (verdicts['f2p'] > 0).any() and (
        verdicts['f2p_not_run'] == verdicts['f2p']
    ).all():
        log.warning(
            'no FAIL_TO_PASS test ran in any agent run: were they run '
            'with --apply-test-patch?'
        )


def main() -> None:
    """Derive, grade, print and write."""
    args = parse_args()
    setup_logging(level=logging.WARNING)

    frame = load(args.data, args.instance)
    sides = frame.groupby(INSTANCE)[PATCH].agg(set)
    one_sided = sorted(i for i, s in sides.items() if not {PRE, POST} <= s)
    if one_sided:
        log.warning(
            'skipped, no before_patch and gold run both with results: '
            f'{", ".join(one_sided)}'
        )
    tests, _ = reference_split(frame, PRE, POST)
    split = split_json(tests)
    print_split(split, args.list)

    verdicts, failures = grade(frame, tests)
    if not verdicts.empty:
        print()
        print_verdicts(verdicts, failures, args.list)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'split.json').write_text(json.dumps(split, indent=1) + '\n')
    written = ['split.json']
    if not verdicts.empty:
        verdicts.to_csv(args.out / 'verdicts.csv', index=False)
        written.append('verdicts.csv')
    print()
    print(f'Wrote {" and ".join(written)} to {args.out}')


if __name__ == '__main__':
    main()
