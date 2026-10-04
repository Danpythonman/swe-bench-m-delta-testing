"""Compare this harness's test split with SWE-bench Verified's labels.

The harness derives FAIL_TO_PASS and PASS_TO_PASS on its own, from
``before_patch`` and ``gold`` runs, with the same classifier used for
SWE-bench M (``notebooks/test_split.py``). The published lists in each
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
    function names; pytest labels are truncated at the first space) and
    the harness classified them differently.

Usage:
    uv run scripts/compare_official_split.py               # synced parquet
    uv run scripts/compare_official_split.py --data results
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Final

import pandas as pd

from sbmdt.benchmark import Benchmark, benchmark_of
from sbmdt.env import PROJECT_BASE
from sbmdt.instance import ReferenceSplit
from sbmdt.log import setup_logging

sys.path.insert(0, str(Path(__file__).parent))
from analyze_results import (  # noqa: E402
    INSTANCE,
    PASSED,
    POST,
    PRE,
    REPO,
    TEST,
    reference_split,
    render,
    repo_of,
)

log = logging.getLogger(__name__)

# Official label -> ReferenceSplit field, which doubles as the harness
# category (lowercased) that agrees with the label.
OFFICIAL: Final[dict[str, str]] = {
    'FAIL_TO_PASS': 'fail_to_pass',
    'PASS_TO_PASS': 'pass_to_pass',
}


def load_verified_results(data_dir: Path) -> pd.DataFrame:
    """Read Parquet and JSON results, keeping SWE-bench Verified rows.

    Parquet is what S3 holds; JSON is what ``run_instance.py --json
    --file`` writes locally, so both are accepted.

    Args:
        data_dir: Directory of result objects.

    Returns:
        A long-format frame with a ``repo`` column added.

    Raises:
        SystemExit: If the directory holds no Verified results.
    """
    frames: list[pd.DataFrame] = []
    parquet = sorted(data_dir.glob('*.parquet'))
    if parquet:
        frames.append(pd.concat(map(pd.read_parquet, parquet)))
    for path in sorted(data_dir.glob('*.json')):
        frames.append(pd.DataFrame(json.loads(path.read_text())))
    if not frames:
        raise SystemExit(f'no results in {data_dir}')
    frame = pd.concat(frames, ignore_index=True)
    verified = (
        frame[INSTANCE].map(benchmark_of) == Benchmark.SWE_BENCH_VERIFIED
    )
    frame = frame[verified].copy()
    if frame.empty:
        raise SystemExit(f'no SWE-bench Verified results in {data_dir}')
    frame[PASSED] = frame[PASSED].astype(bool)
    frame['timestamp'] = pd.to_datetime(frame['timestamp'], utc=True)
    frame[REPO] = frame[INSTANCE].map(repo_of)
    return frame


def matching_key(instance_id: str, test: str) -> str:
    """Return the name a harness test is matched to official labels by.

    The harness names every test unambiguously; the official labels do
    not, in two ways this undoes for matching only:

    - SymPy labels are bare function names, so a SymPy ``path::function``
      id is matched by its function name.
    - pytest labels are cut at the first space (no official pytest label
      contains one), so a parametrized id such as
      ``test_x[a: str = None-str]`` is published as ``test_x[a:``. A
      pytest node id is matched by its text up to the first space.

    Django labels keep their spaces (``method (module.Class)``) and are
    matched as recorded.

    Args:
        instance_id: The instance the test belongs to.
        test: The harness test name.

    Returns:
        The key to look the test up by in the official lists.
    """
    if instance_id.startswith('sympy__sympy'):
        return test.rpartition('::')[2]
    if instance_id.startswith('django__django'):
        return test
    return test.split(' ', 1)[0]


def with_keys(tests: pd.DataFrame) -> pd.DataFrame:
    """Add each harness test's :func:`matching_key` as a ``key`` column.

    Args:
        tests: The per-test frame from ``reference_split``.

    Returns:
        A copy of ``tests`` with a ``key`` column.
    """
    keys = [
        matching_key(instance_id, test)
        for instance_id, test in zip(tests[INSTANCE], tests[TEST], strict=True)
    ]
    return tests.assign(key=keys)


def compare(tests: pd.DataFrame) -> pd.DataFrame:
    """Line up every official F2P/P2P test with the harness's verdict.

    Args:
        tests: The per-test frame from :func:`with_keys`, one row per
            test the harness classified, with a ``category`` column.

    Returns:
        One row per official test, with the official ``label`` and the
        harness ``observed`` category: ``not_run`` when no harness test
        matches, ``ambiguous`` when several do and they disagree.
    """
    observed: dict[tuple[str, str], str] = {}
    for instance_id, key, category in zip(
        tests[INSTANCE], tests['key'], tests['category'], strict=True
    ):
        category = str(category).lower()
        previous = observed.setdefault((instance_id, key), category)
        if previous != category:
            observed[(instance_id, key)] = 'ambiguous'
    rows: list[dict[str, str]] = []
    for instance_id in sorted(tests[INSTANCE].unique()):
        reference = ReferenceSplit.load(instance_id)
        for label, field in OFFICIAL.items():
            for test in getattr(reference, field):
                rows.append(
                    {
                        INSTANCE: instance_id,
                        REPO: repo_of(instance_id),
                        TEST: test,
                        'label': label,
                        'observed': observed.get(
                            (instance_id, test), 'not_run'
                        ),
                    }
                )
    return pd.DataFrame(rows)


def extra_fail_to_pass(
    tests: pd.DataFrame, official: pd.DataFrame
) -> pd.DataFrame:
    """Return tests the harness calls FAIL_TO_PASS but the benchmark does not.

    Args:
        tests: The per-test frame from :func:`with_keys`.
        official: Output of :func:`compare`.

    Returns:
        The harness's F2P rows absent from the official F2P list.
    """
    ours = tests[tests['category'] == 'FAIL_TO_PASS'][[INSTANCE, TEST, 'key']]
    theirs = official[official['label'] == 'FAIL_TO_PASS'][[INSTANCE, TEST]]
    merged = ours.merge(
        theirs.rename(columns={TEST: 'key'}), how='left', indicator=True
    )
    extra = merged[merged['_merge'] == 'left_only'].drop(
        columns=['_merge', 'key']
    )
    return extra.assign(**{REPO: extra[INSTANCE].map(repo_of)})


def per_instance(official: pd.DataFrame, extra: pd.DataFrame) -> pd.DataFrame:
    """Summarise agreement for each instance.

    An instance *matches* when every official F2P test is F2P for the
    harness, every official P2P test is P2P, and the harness finds no
    F2P test the benchmark lacks.

    Args:
        official: Output of :func:`compare`.
        extra: Output of :func:`extra_fail_to_pass`.

    Returns:
        One row per instance.
    """
    agrees = official['observed'] == official['label'].map(OFFICIAL)
    summary = (
        official.assign(agrees=agrees)
        .groupby([REPO, INSTANCE, 'label'])['agrees']
        .agg(['sum', 'count'])
        .unstack('label', fill_value=0)
    )
    summary.columns = [
        f'{label.lower()}_{stat}'.replace('sum', 'agree').replace(
            'count', 'official'
        )
        for stat, label in summary.columns
    ]
    summary = summary.reset_index()
    counts = extra.groupby(INSTANCE).size()
    summary['extra_f2p'] = summary[INSTANCE].map(counts).fillna(0).astype(int)
    summary['match'] = (
        (summary['fail_to_pass_agree'] == summary['fail_to_pass_official'])
        & (summary['pass_to_pass_agree'] == summary['pass_to_pass_official'])
        & (summary['extra_f2p'] == 0)
    )
    return summary


def per_repo(summary: pd.DataFrame) -> pd.DataFrame:
    """Roll the per-instance summary up to repositories.

    Args:
        summary: Output of :func:`per_instance`.

    Returns:
        One row per repository; ``render`` adds the total row.
    """
    columns = [
        'fail_to_pass_agree',
        'fail_to_pass_official',
        'pass_to_pass_agree',
        'pass_to_pass_official',
        'extra_f2p',
        'match',
    ]
    table = summary.groupby(REPO)[columns].sum()
    table.insert(0, 'instances', summary.groupby(REPO).size())
    return table.astype(int)


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
