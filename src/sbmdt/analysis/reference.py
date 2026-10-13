"""Reference split and model scoring over synced SWE-bench M results.

``reference_split`` classifies every test of every instance that has runs
on both sides of its reference patch (``before_patch`` against ``gold``)
with :func:`sbmdt.analysis.test_split.classify_tests`, and adds presence:
whether the patch *introduced* a test, which the classifier alone does not
report. ``score_variant`` then looks a model patch's runs up against that
split, which is what SWE-bench measures.

``scripts/analyze_results.py`` is the command-line front end.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final

import pandas as pd

from sbmdt.analysis.test_split import classify_tests
from sbmdt.benchmark import Benchmark, benchmark_of
from sbmdt.parquet import read_test_results

log = logging.getLogger(__name__)

INSTANCE: Final[str] = 'instance_id'
PATCH: Final[str] = 'patch_type'
TEST: Final[str] = 'test_name'
FILE: Final[str] = 'test_file'
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

    Only SWE-bench M rows are kept. SWE-bench Verified results can share
    the bucket, but they have their own reference labels and comparison
    (``scripts/compare_official_split.py``), and mixing them in would
    change every figure here, including the ``--self-check`` counts.

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
    frame = read_test_results(data_dir)
    is_m = frame[INSTANCE].map(benchmark_of) == Benchmark.SWE_BENCH_M
    frame = frame[is_m].copy()
    frame[PASSED] = frame[PASSED].astype(bool)
    frame[REPO] = frame[INSTANCE].map(repo_of)
    return disambiguate_test_names(frame)


def disambiguate_test_names(frame: pd.DataFrame) -> pd.DataFrame:
    """Qualify test names that more than one file defines.

    Runners name tests by their suite path alone, so two files can define
    the same name (carbon has a "Public API should only change with a
    semver change" test in several packages). Merged under one name, a
    failing and a passing test read as a single flaky test and are
    quarantined. Where results carry ``test_file``, a name that occurs in
    more than one file within an instance becomes ``file::name``. Every
    other name is left alone, so results without file information (older
    runs, agent runs) still line up with it.

    Args:
        frame: The results frame, with an optional ``test_file`` column.

    Returns:
        The frame with colliding test names qualified.
    """
    if FILE not in frame or frame[FILE].isna().all():
        return frame
    known = frame[frame[FILE].notna()]
    files_per_name = known.groupby([INSTANCE, TEST], observed=True)[
        FILE
    ].nunique()
    colliding = files_per_name[files_per_name > 1].index
    if colliding.empty:
        return frame
    keys = pd.MultiIndex.from_arrays([frame[INSTANCE], frame[TEST]])
    qualify = keys.isin(colliding) & frame[FILE].notna().to_numpy()
    frame = frame.copy()
    frame.loc[qualify, TEST] = (
        frame.loc[qualify, FILE] + '::' + frame.loc[qualify, TEST]
    )
    log.info(
        f'qualified {len(colliding)} test name(s) defined in several files'
    )
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
    keep = both_sides(frame, pre, post)
    if not keep:
        raise SystemExit(f'no instance has both {pre} and {post} runs')

    subset = frame[
        frame[PATCH].isin([pre, post]) & frame[INSTANCE].isin(keep)
    ].copy()
    # A repaired reference run must replace, not merge with, an older run.
    # Mixing timestamps makes deterministic before/gold changes appear flaky
    # and can leave a stale zero-length FAIL_TO_PASS split after a rerun.
    if {'agent_name', 'timestamp'} <= set(subset.columns):
        latest = subset.groupby(
            [INSTANCE, PATCH, 'agent_name'], observed=True
        )['timestamp'].transform('max')
        subset = subset[subset['timestamp'] == latest].copy()
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
    frame: pd.DataFrame,
    variant: str,
    agent: str | None = None,
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
    subset = frame[frame[PATCH] == variant].copy()
    if agent is not None:
        subset = subset[subset['agent_name'] == agent].copy()
        if not subset.empty:
            latest = subset.groupby(INSTANCE)['timestamp'].transform('max')
            subset = subset[subset['timestamp'] == latest]
    statuses = _model_statuses(subset)
    values = statuses['status'].map(
        {'passed': True, 'failed': False, 'flaky': None}
    )
    return dict(
        zip(
            zip(statuses[INSTANCE], statuses[TEST], strict=True),
            values,
            strict=True,
        )
    )


def _model_statuses(subset: pd.DataFrame) -> pd.DataFrame:
    """Vectorized per-test model status used by the report scorer."""
    agg = (
        subset.groupby([INSTANCE, TEST], observed=True)[PASSED]
        .agg(['min', 'max'])
        .reset_index()
    )
    same = agg['min'] == agg['max']
    agg['status'] = 'flaky'
    agg.loc[same & agg['min'].astype(bool), 'status'] = 'passed'
    agg.loc[same & ~agg['min'].astype(bool), 'status'] = 'failed'
    return agg[[INSTANCE, TEST, 'status']]


def score_variant(
    frame: pd.DataFrame,
    tests: pd.DataFrame,
    variant: str,
    agent: str | None = None,
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
    model_runs = frame[frame[PATCH] == variant].copy()
    if agent is not None:
        model_runs = model_runs[model_runs['agent_name'] == agent].copy()
        if not model_runs.empty:
            latest = model_runs.groupby(INSTANCE)['timestamp'].transform('max')
            model_runs = model_runs[model_runs['timestamp'] == latest]
    ran = set(model_runs[INSTANCE].unique())
    scope = ran & set(tests[INSTANCE])

    scored = tests[
        tests[INSTANCE].isin(scope)
        & tests['category'].isin(['FAIL_TO_PASS', 'PASS_TO_PASS'])
    ].copy()

    statuses = _model_statuses(model_runs)
    scored = scored.merge(statuses, on=[INSTANCE, TEST], how='left')
    scored['status'] = scored['status'].fillna('not_run')
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
