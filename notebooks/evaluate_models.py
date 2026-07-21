"""Score model test runs against precomputed F2P / P2P splits.

Consumes the `test_splits.json` produced by the F2P/P2P construction
notebook, plus a frame of model test runs sharing the same schema, and
reports per-instance resolution and per-agent resolve rates.

The scoring convention is deliberately strict and mirrors SWE-bench: an
instance is resolved only if every FAIL_TO_PASS and every PASS_TO_PASS
test passes. A test with no row in the run data counts as a failure —
unlike in split construction, a missing verdict here means the harness
crashed, timed out, or failed collection, not that the test is new.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pandas as pd

# --- Public API ---

__all__: list[str] = [
    'collapse_runs',
    'load_splits',
    'score_instances',
    'summarize',
]

# --- Constants / module-level variables ---

INSTANCE: str = 'instance_id'
AGENT: str = 'agent_name'
TEST: str = 'test_name'
PASSED: str = 'passed'

F2P: str = 'FAIL_TO_PASS'
P2P: str = 'PASS_TO_PASS'

Splits = dict[str, dict[str, list[str]]]

# --- Functions ---


def load_splits(
    path: str | Path,
    require_f2p: bool = True,
) -> Splits:
    """Load exported splits, dropping unusable instances.

    An instance with no FAIL_TO_PASS test demonstrates nothing about
    the bug being fixed, so scoring it would hand every model a free
    resolution on its PASS_TO_PASS tests alone.

    Args:
        path: Path to the exported test_splits.json.
        require_f2p: Drop instances carrying no FAIL_TO_PASS tests.

    Returns:
        A mapping of instance id to its F2P / P2P test-name lists.
    """
    payload: Splits = json.loads(Path(path).read_text())
    if not require_f2p:
        return payload
    return {inst: tests for inst, tests in payload.items() if tests.get(F2P)}


def collapse_runs(df: pd.DataFrame) -> pd.DataFrame:
    """Reduce repeat runs to one strict verdict per cell.

    A test passes only if it passed in every run, matching the rule
    used to build the splits. `unstable` flags cells where runs
    disagreed — flakiness the gold-side quarantine could not see,
    because it only observed the pre/post patch types.

    Args:
        df: Model run rows, one per (agent, instance, test, attempt).

    Returns:
        One row per (agent, instance, test) with `passed`, `n_runs`,
        and `unstable` columns.
    """

    def _unstable(s: pd.Series) -> bool:
        return bool(s.any() != s.all())

    grouped = df.groupby([AGENT, INSTANCE, TEST], observed=True)[PASSED]
    return cast(
        pd.DataFrame,
        grouped.agg(
            passed='all',
            n_runs='size',
            unstable=_unstable,
        ).reset_index(),
    )


def _tally(
    tests: list[str],
    passing: set[str],
    observed: set[str],
) -> tuple[int, int, int]:
    """Count outcomes for one category of tests.

    Args:
        tests: Test names expected for this instance and category.
        passing: Test names the agent passed on this instance.
        observed: Test names the agent produced any row for.

    Returns:
        A tuple of (total, passed, missing) counts.
    """
    n_passed = sum(t in passing for t in tests)
    n_missing = sum(t not in observed for t in tests)
    return len(tests), n_passed, n_missing


def score_instances(
    runs: pd.DataFrame,
    splits: Splits,
) -> pd.DataFrame:
    """Score every agent against every instance in the splits.

    Agents are scored on all instances, not just those they emitted
    rows for: a no-show is a failure, not an absence.

    Args:
        runs: Collapsed verdicts from `collapse_runs`.
        splits: Mapping from `load_splits`.

    Returns:
        One row per (agent, instance) with per-category counts and a
        boolean `resolved`.
    """
    passing: dict[str, set[tuple[str, str]]] = {}
    observed: dict[str, set[tuple[str, str]]] = {}
    for row in runs.itertuples(index=False):
        agent = str(getattr(row, AGENT))
        cell = (str(getattr(row, INSTANCE)), str(getattr(row, TEST)))
        observed.setdefault(agent, set()).add(cell)
        if getattr(row, PASSED):
            passing.setdefault(agent, set()).add(cell)

    records: list[dict[str, object]] = []
    for agent in sorted(observed):
        for inst, tests in splits.items():
            seen = {t for i, t in observed[agent] if i == inst}
            won = {t for i, t in passing.get(agent, set()) if i == inst}
            f2p_n, f2p_ok, f2p_gone = _tally(tests[F2P], won, seen)
            p2p_n, p2p_ok, p2p_gone = _tally(tests[P2P], won, seen)
            records.append(
                {
                    AGENT: agent,
                    INSTANCE: inst,
                    'f2p_total': f2p_n,
                    'f2p_passed': f2p_ok,
                    'p2p_total': p2p_n,
                    'p2p_passed': p2p_ok,
                    'missing': f2p_gone + p2p_gone,
                    'resolved': f2p_ok == f2p_n and p2p_ok == p2p_n,
                }
            )
    return pd.DataFrame.from_records(records)


def summarize(scored: pd.DataFrame) -> pd.DataFrame:
    """Aggregate per-instance scores into per-agent rates.

    The three rates separate failure modes: `f2p_rate` says whether the
    patch fixes anything, `p2p_rate` says whether it breaks anything,
    and `resolve_rate` is the headline number requiring both.

    Args:
        scored: Frame returned by `score_instances`.

    Returns:
        One row per agent, sorted by resolve rate descending.
    """
    grouped = scored.groupby(AGENT, observed=True)
    out = cast(
        pd.DataFrame,
        grouped.agg(
            instances=('resolved', 'size'),
            resolved=('resolved', 'sum'),
            f2p_total=('f2p_total', 'sum'),
            f2p_passed=('f2p_passed', 'sum'),
            p2p_total=('p2p_total', 'sum'),
            p2p_passed=('p2p_passed', 'sum'),
            missing=('missing', 'sum'),
        ),
    )
    out['resolve_rate'] = out['resolved'] / out['instances']
    out['f2p_rate'] = out['f2p_passed'] / out['f2p_total']
    out['p2p_rate'] = out['p2p_passed'] / out['p2p_total']
    return out.sort_values('resolve_rate', ascending=False)
