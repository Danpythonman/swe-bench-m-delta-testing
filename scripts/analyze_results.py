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

The classifier itself lives in ``notebooks/test_split.py`` and is used
as-is, so both this script and the notebook agree by construction. What
this script adds is presence: ``classify_tests`` reports a verdict per
test, but not whether the patch *introduced* the test, and the two
together are what the per-repository table needs.

Usage:
    uv run scripts/analyze_results.py reference
    uv run scripts/analyze_results.py reference --self-check
    uv run scripts/analyze_results.py score --variant without_image
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Final

import pandas as pd

from sbmdt.env import PROJECT_BASE
from sbmdt.log import setup_logging

log = logging.getLogger(__name__)

INSTANCE: Final[str] = 'instance_id'
PATCH: Final[str] = 'patch_type'
TEST: Final[str] = 'test_name'
PASSED: Final[str] = 'passed'
REPO: Final[str] = 'repo'

PRE: Final[str] = 'before_patch'
POST: Final[str] = 'gold'
VARIANTS: Final[tuple[str, ...]] = ('with_image', 'without_image')

# What the published reference report reports, used by --self-check.
EXPECTED: Final[dict[str, int]] = {
    'instances': 176,
    'with_f2p': 78,
    'f2p': 1395,
    'p2p': 490719,
    'added': 2628,
    'added_failing': 1221,
    'flaky_added': 26,
    'regressed': 180,
}


def load_classifier() -> Any:
    """Import ``classify_tests`` from the notebooks package by path.

    Returns:
        The ``classify_tests`` function.

    Raises:
        ImportError: If the module cannot be loaded.
    """
    path = PROJECT_BASE / 'notebooks' / 'test_split.py'
    spec = importlib.util.spec_from_file_location('test_split', path)
    if spec is None or spec.loader is None:
        raise ImportError(f'cannot load {path}')
    module = importlib.util.module_from_spec(spec)
    sys.modules['test_split'] = module
    spec.loader.exec_module(module)
    return module.classify_tests


def repo_of(instance_id: str) -> str:
    """Extract the ``org__repo`` prefix from an instance id.

    Args:
        instance_id: The full benchmark instance id.

    Returns:
        The repository prefix.
    """
    return instance_id.rsplit('-', 1)[0]


def short_repo(repo: str) -> str:
    """Shorten an ``org__repo`` prefix to just the repository name.

    Args:
        repo: The full prefix.

    Returns:
        The repository name without its owner.
    """
    return repo.split('__')[-1]


def load_results(data_dir: Path) -> pd.DataFrame:
    """Read every synced Parquet result into one frame.

    Args:
        data_dir: Directory of synced Parquet objects.

    Returns:
        A long-format frame with a ``repo`` column added.

    Raises:
        FileNotFoundError: If ``data_dir`` does not exist.
    """
    if not data_dir.is_dir():
        raise FileNotFoundError(
            f'no such directory: {data_dir}. '
            'Run aws/sync-s3-with-local.sh first.'
        )
    frame = pd.read_parquet(data_dir)
    frame[PASSED] = frame[PASSED].astype(bool)
    frame[REPO] = frame[INSTANCE].map(repo_of)
    return frame


def both_sides(frame: pd.DataFrame, pre: str, post: str) -> set[str]:
    """List the instances that have runs under both patch types.

    An instance missing one side cannot be classified: a test with no
    pre-patch row reads as fail -> pass, so an instance with no pre-patch
    runs at all would report its whole passing suite as FAIL_TO_PASS.

    Args:
        frame: The full results frame.
        pre: patch_type marking the pre-patch runs.
        post: patch_type marking the post-patch runs.

    Returns:
        The instance ids present under both patch types.
    """
    subset = frame[frame[PATCH].isin([pre, post])]
    sides = subset.groupby(INSTANCE, observed=True)[PATCH].nunique()
    return set(sides[sides == 2].index)


def presence(frame: pd.DataFrame, pre: str, post: str) -> pd.DataFrame:
    """Mark each (instance, test) as added, removed, or present on both sides.

    Presence is about which side a test *appears* on, independent of
    whether it passed. A test the patch introduces has no pre-patch row
    to compare against, which is what separates a test the patch added
    from one it fixed.

    Args:
        frame: The results frame, already limited to classifiable instances.
        pre: patch_type marking the pre-patch runs.
        post: patch_type marking the post-patch runs.

    Returns:
        A frame of (instance_id, test_name, added, removed) rows.
    """
    pre_tests = frame.loc[frame[PATCH] == pre, [INSTANCE, TEST]]
    post_tests = frame.loc[frame[PATCH] == post, [INSTANCE, TEST]]
    pre_keys = set(map(tuple, pre_tests.drop_duplicates().to_numpy()))
    post_keys = set(map(tuple, post_tests.drop_duplicates().to_numpy()))

    rows = [
        {
            INSTANCE: instance,
            TEST: test,
            'added': (instance, test) not in pre_keys,
            'removed': (instance, test) not in post_keys,
        }
        for instance, test in pre_keys | post_keys
    ]
    return pd.DataFrame.from_records(rows)


def mapping_to_frame(mapping: dict[str, list[str]]) -> pd.DataFrame:
    """Flatten an instance -> test-names mapping into long rows.

    Args:
        mapping: Per-instance test names, as the classifier returns.

    Returns:
        A frame of (instance_id, test_name, repo) rows.
    """
    records = [
        {INSTANCE: instance, TEST: name}
        for instance, names in sorted(mapping.items())
        for name in names
    ]
    frame = pd.DataFrame.from_records(records, columns=[INSTANCE, TEST])
    frame[REPO] = frame[INSTANCE].map(repo_of) if not frame.empty else []
    return frame


def reference_split(
    frame: pd.DataFrame, pre: str, post: str
) -> tuple[pd.DataFrame, Any]:
    """Classify every test in the instances that have both sides.

    Args:
        frame: The full results frame.
        pre: patch_type marking the pre-patch runs.
        post: patch_type marking the post-patch runs.

    Returns:
        A ``(tests, split)`` pair. ``tests`` has one row per classified
        test, tagged with its category and whether the patch added it.
        ``split`` is the raw ``TestSplit`` from the classifier.

    Raises:
        SystemExit: If no instance has runs on both sides.
    """
    classify_tests = load_classifier()
    keep = both_sides(frame, pre, post)
    if not keep:
        raise SystemExit(f'no instance has both {pre} and {post} runs')

    subset = frame[
        frame[PATCH].isin([pre, post]) & frame[INSTANCE].isin(keep)
    ].copy()
    split = classify_tests(subset, pre_label=pre, post_label=post)

    categories = {
        'FAIL_TO_PASS': split.fail_to_pass,
        'PASS_TO_PASS': split.pass_to_pass,
        'REGRESSED': split.regressed,
        'BROKEN': split.broken,
        'FLAKY': split.flaky,
    }
    parts = []
    for label, mapping in categories.items():
        part = mapping_to_frame(mapping)
        if part.empty:
            continue
        part['category'] = label
        parts.append(part)
    tests = pd.concat(parts, ignore_index=True)

    marks = presence(subset, pre, post)
    tests = tests.merge(marks, on=[INSTANCE, TEST], how='left')
    tests['added'] = tests['added'].fillna(False)
    tests['removed'] = tests['removed'].fillna(False)
    return tests, split


def reference_table(tests: pd.DataFrame) -> pd.DataFrame:
    """Build the per-repository table from the classified tests.

    Columns match the published report: instances classified, instances
    with any FAIL_TO_PASS test, the two headline categories, tests the
    patch added, how many of those still fail, how many were quarantined
    as flaky, and pass -> fail regressions.

    Args:
        tests: The classified tests, as ``reference_split`` returns.

    Returns:
        One row per repository, sorted by FAIL_TO_PASS descending.
    """
    f2p = tests[tests['category'] == 'FAIL_TO_PASS']
    p2p = tests[tests['category'] == 'PASS_TO_PASS']
    regressed = tests[tests['category'] == 'REGRESSED']
    added = tests[tests['added']]

    table = pd.DataFrame(
        {
            'instances': tests.groupby(REPO)[INSTANCE].nunique(),
            'with_f2p': f2p.groupby(REPO)[INSTANCE].nunique(),
            'f2p': f2p.groupby(REPO).size(),
            'p2p': p2p.groupby(REPO).size(),
            'added': added.groupby(REPO).size(),
            'added_failing': added[added['category'] == 'BROKEN']
            .groupby(REPO)
            .size(),
            'flaky_added': added[added['category'] == 'FLAKY']
            .groupby(REPO)
            .size(),
            'regressed': regressed.groupby(REPO).size(),
        }
    )
    return table.fillna(0).astype(int).sort_values('f2p', ascending=False)


def model_verdicts(
    frame: pd.DataFrame, variant: str
) -> dict[Any, bool | None]:
    """Collapse a model patch's runs to one verdict per test.

    A test counts as passed only if it passed in every run of that patch.
    Disagreement across repeated runs is quarantined rather than resolved,
    since a flaky pass is not evidence the patch works.

    Args:
        frame: The full results frame.
        variant: The model patch_type to collapse.

    Returns:
        (instance_id, test_name) -> True (passed), False (failed), or
        None (flaky).
    """
    subset = frame[frame[PATCH] == variant]
    agg = subset.groupby([INSTANCE, TEST])[PASSED].agg(['min', 'max'])
    return {
        key: (bool(row['min']) if row['min'] == row['max'] else None)
        for key, row in agg.iterrows()
    }


def score_variant(
    frame: pd.DataFrame, tests: pd.DataFrame, variant: str
) -> pd.DataFrame:
    """Look up each reference test in a model patch's runs.

    Only instances that have both a reference split and a run under
    ``variant`` can be scored.

    Args:
        frame: The full results frame.
        tests: The classified reference tests.
        variant: The model patch_type to score.

    Returns:
        The reference FAIL_TO_PASS and PASS_TO_PASS tests for the scorable
        instances, each tagged ``passed``, ``failed``, ``not_run`` or
        ``flaky`` under the model patch.
    """
    verdicts = model_verdicts(frame, variant)
    ran = set(frame.loc[frame[PATCH] == variant, INSTANCE].unique())
    scope = ran & set(tests[INSTANCE])

    scored = tests[
        tests[INSTANCE].isin(scope)
        & tests['category'].isin(['FAIL_TO_PASS', 'PASS_TO_PASS'])
    ].copy()

    def status(instance: str, test: str) -> str:
        key = (instance, test)
        if key not in verdicts:
            return 'not_run'
        verdict = verdicts[key]
        if verdict is None:
            return 'flaky'
        return 'passed' if verdict else 'failed'

    scored['status'] = [
        status(i, t)
        for i, t in zip(scored[INSTANCE], scored[TEST], strict=True)
    ]
    scored['variant'] = variant
    return scored


def score_table(scored: pd.DataFrame) -> pd.DataFrame:
    """Summarise scored reference tests per repository.

    Args:
        scored: The output of ``score_variant``.

    Returns:
        One row per repository.
    """
    f2p = scored[scored['category'] == 'FAIL_TO_PASS']
    p2p = scored[scored['category'] == 'PASS_TO_PASS']

    def counts(frame: pd.DataFrame, prefix: str) -> dict[str, pd.Series]:
        return {
            f'{prefix}': frame.groupby(REPO).size(),
            f'{prefix}_passed': frame[frame['status'] == 'passed']
            .groupby(REPO)
            .size(),
            f'{prefix}_failed': frame[frame['status'] == 'failed']
            .groupby(REPO)
            .size(),
            f'{prefix}_not_run': frame[frame['status'] == 'not_run']
            .groupby(REPO)
            .size(),
        }

    table = pd.DataFrame(
        {
            'instances': scored.groupby(REPO)[INSTANCE].nunique(),
            **counts(f2p, 'f2p'),
            **counts(p2p, 'p2p'),
        }
    )
    return table.fillna(0).astype(int).sort_values('f2p', ascending=False)


def resolved(scored: pd.DataFrame) -> pd.DataFrame:
    """Decide, per instance, whether the model patch resolved it.

    An instance is resolved when every one of its reference FAIL_TO_PASS
    tests passes and no PASS_TO_PASS test is lost. An instance with no
    FAIL_TO_PASS test cannot be resolved: nothing would demonstrate the
    fix.

    Args:
        scored: The output of ``score_variant``.

    Returns:
        One row per instance with its counts and a ``resolved`` flag.
    """
    rows = []
    for instance, group in scored.groupby(INSTANCE):
        f2p = group[group['category'] == 'FAIL_TO_PASS']
        p2p = group[group['category'] == 'PASS_TO_PASS']
        f2p_ok = len(f2p) > 0 and bool((f2p['status'] == 'passed').all())
        p2p_ok = bool((p2p['status'] == 'passed').all()) if len(p2p) else True
        rows.append(
            {
                INSTANCE: instance,
                'variant': group['variant'].iloc[0],
                'f2p': len(f2p),
                'f2p_passed': int((f2p['status'] == 'passed').sum()),
                'f2p_not_run': int((f2p['status'] == 'not_run').sum()),
                'p2p': len(p2p),
                'p2p_passed': int((p2p['status'] == 'passed').sum()),
                'p2p_failed': int((p2p['status'] == 'failed').sum()),
                'resolved': f2p_ok and p2p_ok,
            }
        )
    return pd.DataFrame.from_records(rows).sort_values(INSTANCE)


def render(table: pd.DataFrame) -> str:
    """Format a per-repository table for the terminal.

    Args:
        table: A table indexed by repository prefix.

    Returns:
        The formatted table, with a totals row appended.
    """
    display = table.copy()
    display.index = [short_repo(r) for r in display.index]
    totals = display.sum()
    totals.name = 'TOTAL'
    return pd.concat([display, totals.to_frame().T]).to_string()


def check(tests: pd.DataFrame) -> int:
    """Compare the reference split against the published report.

    Args:
        tests: The classified tests.

    Returns:
        0 when every figure matches, 1 otherwise.
    """
    added = tests[tests['added']]
    actual = {
        'instances': tests[INSTANCE].nunique(),
        'with_f2p': tests[tests['category'] == 'FAIL_TO_PASS'][
            INSTANCE
        ].nunique(),
        'f2p': int((tests['category'] == 'FAIL_TO_PASS').sum()),
        'p2p': int((tests['category'] == 'PASS_TO_PASS').sum()),
        'added': len(added),
        'added_failing': int((added['category'] == 'BROKEN').sum()),
        'flaky_added': int((added['category'] == 'FLAKY').sum()),
        'regressed': int((tests['category'] == 'REGRESSED').sum()),
    }
    failures = 0
    for key, want in EXPECTED.items():
        got = actual[key]
        ok = got == want
        failures += not ok
        log.info(
            f'  {key:<14} {got:>8,} '
            f'{"matches" if ok else f"DIFFERS from published {want:,}"}'
        )
    return 1 if failures else 0


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
    log.info(
        f'{len(frame):,} rows over {frame[INSTANCE].nunique()} instances'
    )

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
