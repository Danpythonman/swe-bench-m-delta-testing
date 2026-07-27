"""Derive FAIL_TO_PASS and PASS_TO_PASS test sets per instance.

Takes a long-format frame of individual test results spanning pre-patch
and post-patch runs, collapses repeated runs into one verdict per test,
quarantines flaky tests, and classifies the rest by their pre -> post
outcome transition.

The `agent_name` and `timestamp` columns are deliberately ignored: they
are treated as nothing more than repetitions of the same
(instance, patch_type, test_name) cell. If different agents represent
different candidate patches rather than repeat runs, filter to the
gold/reference agent before calling `classify_tests`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import cast

import pandas as pd

# --- Public API ---

__all__: list[str] = [
    'ClassificationError',
    'Columns',
    'MissingColumnError',
    'MissingPatchTypeError',
    'TestSplit',
    'classify_tests',
]

# --- Constants / module-level variables ---

logger = logging.getLogger(__name__)

# Internal column names used only between the private helpers.
_ALL_PASSED: str = '_all_passed'
_ANY_PASSED: str = '_any_passed'
_MERGE: str = '_merge'

# --- Exceptions ---


class ClassificationError(Exception):
    """Base exception for test classification failures."""


class MissingColumnError(ClassificationError):
    """Raised when the input frame lacks a required column."""


class MissingPatchTypeError(ClassificationError):
    """Raised when the pre- or post-patch label is absent."""


# --- Classes ---


@dataclass(frozen=True)
class Columns:
    """Column names the classifier reads from the input frame.

    Override any field if the source frame uses a different schema.
    """

    instance: str = 'instance_id'
    patch_type: str = 'patch_type'
    test_name: str = 'test_name'
    passed: str = 'passed'

    @property
    def required(self) -> list[str]:
        """Every column that must be present on the input frame."""
        return [
            self.instance,
            self.patch_type,
            self.test_name,
            self.passed,
        ]

    @property
    def key(self) -> list[str]:
        """Columns identifying a single test within an instance."""
        return [self.instance, self.test_name]


@dataclass(frozen=True)
class TestSplit:
    """Per-instance test sets keyed by instance id.

    Attributes:
        fail_to_pass: Failed pre-patch, passed post-patch.
        pass_to_pass: Passed both pre- and post-patch.
        regressed: Passed pre-patch, failed post-patch. Always worth
            inspecting: it signals a bad patch or a dirty environment.
        broken: Failed both pre- and post-patch. Carries no signal.
        flaky: Verdict disagreed across runs, so it is untrustworthy.
    """

    fail_to_pass: dict[str, list[str]]
    pass_to_pass: dict[str, list[str]]
    regressed: dict[str, list[str]]
    broken: dict[str, list[str]]
    flaky: dict[str, list[str]]


# --- Functions ---


def _validate(
    frame: pd.DataFrame,
    columns: Columns,
    pre_label: str,
    post_label: str,
) -> None:
    """Check the frame has the required schema and patch labels.

    Args:
        frame: The raw test-result frame.
        columns: The column-name mapping.
        pre_label: Value of the patch_type column for pre-patch runs.
        post_label: Value of the patch_type column for post-patch runs.

    Raises:
        MissingColumnError: If a required column is absent.
        MissingPatchTypeError: If a patch label never appears.
    """
    missing = [c for c in columns.required if c not in frame.columns]
    if missing:
        raise MissingColumnError(f'missing columns: {missing}')

    patch_type = cast(pd.Series, frame[columns.patch_type])
    present = set(patch_type.unique())
    unknown = {pre_label, post_label} - present
    if unknown:
        raise MissingPatchTypeError(
            f'patch_type never takes value(s) {sorted(unknown)}; '
            f'found {sorted(present)}'
        )


def _collapse_runs(frame: pd.DataFrame, columns: Columns) -> pd.DataFrame:
    """Reduce repeated runs of a test to a single pair of verdicts.

    A test is considered passing only if it passed in every run. The
    disagreement between the all- and any- aggregations is precisely
    what exposes flakiness downstream.

    Args:
        frame: The raw test-result frame.
        columns: The column-name mapping.

    Returns:
        One row per (instance, patch_type, test) with _all_passed and
        _any_passed boolean columns.
    """
    keys = [columns.instance, columns.patch_type, columns.test_name]
    passed = cast(pd.Series, frame[columns.passed]).astype(bool)
    return cast(
        pd.DataFrame,
        (
            frame.assign(**{columns.passed: passed})
            .groupby(keys, dropna=False, observed=True)[columns.passed]
            .agg(**{_ALL_PASSED: 'all', _ANY_PASSED: 'any'})
            .reset_index()
        ),
    )


def _split_flaky(
    status: pd.DataFrame,
    columns: Columns,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Separate tests whose verdict was inconsistent across runs.

    A test that is flaky under either patch type is untrustworthy under
    both, so the whole (instance, test) pair is quarantined.

    Args:
        status: Output of `_collapse_runs`.
        columns: The column-name mapping.

    Returns:
        A (stable, flaky) pair of frames. `flaky` holds the distinct
        (instance, test) pairs that were dropped.
    """
    all_passed = cast(pd.Series, status[_ALL_PASSED])
    any_passed = cast(pd.Series, status[_ANY_PASSED])
    inconsistent = all_passed != any_passed
    flaky = cast(
        pd.DataFrame,
        status.loc[inconsistent, columns.key].drop_duplicates(),
    )

    marked = status.merge(
        flaky,
        on=columns.key,
        how='left',
        indicator=True,
    )
    merge_indicator = cast(pd.Series, marked[_MERGE])
    stable = cast(
        pd.DataFrame,
        marked.loc[merge_indicator == 'left_only'].drop(
            columns=[_MERGE, _ANY_PASSED],
        ),
    )
    return stable, flaky


def _pivot_patch_status(
    stable: pd.DataFrame,
    columns: Columns,
    pre_label: str,
    post_label: str,
) -> pd.DataFrame:
    """Lay the pre- and post-patch verdicts side by side.

    Args:
        stable: Non-flaky rows from `_split_flaky`.
        columns: The column-name mapping.
        pre_label: Value of the patch_type column for pre-patch runs.
        post_label: Value of the patch_type column for post-patch runs.

    Returns:
        One row per (instance, test) with boolean pre and post columns.
    """
    wide = (
        stable.pivot(
            index=columns.key,
            columns=columns.patch_type,
            values=_ALL_PASSED,
        )
        # reindex guarantees both columns exist even if a label was
        # entirely eliminated by the flake filter.
        .reindex(columns=[pre_label, post_label])
        .reset_index()
    )

    # A test the patch introduced has no pre-patch run. Absent is
    # equivalent to not passing, which makes it a FAIL_TO_PASS.
    wide[pre_label] = wide[pre_label].fillna(False).astype(bool)

    # Without a post-patch verdict there is nothing to classify: the
    # patch deleted, renamed, or skipped the test.
    wide = wide.dropna(subset=[post_label])
    wide[post_label] = wide[post_label].astype(bool)
    return wide


def _to_mapping(frame: pd.DataFrame, columns: Columns) -> dict[str, list[str]]:
    """Collect test names into a sorted list per instance.

    Args:
        frame: Any frame carrying the instance and test-name columns.
        columns: The column-name mapping.

    Returns:
        A mapping of instance id to sorted test names. Instances with
        no matching tests are omitted.
    """
    if frame.empty:
        return {}
    grouped = frame.groupby(columns.instance, observed=True)
    return {
        str(key): sorted(names) for key, names in grouped[columns.test_name]
    }


def classify_tests(
    frame: pd.DataFrame,
    pre_label: str,
    post_label: str,
    columns: Columns | None = None,
) -> TestSplit:
    """Build FAIL_TO_PASS and PASS_TO_PASS test sets per instance.

    Args:
        frame: Long-format test results, one row per test execution.
        pre_label: Value of the patch_type column marking pre-patch
            runs (the buggy state).
        post_label: Value of the patch_type column marking post-patch
            runs (the fixed state).
        columns: Column-name mapping. Defaults to the standard schema.

    Returns:
        A TestSplit holding the two headline sets plus the discarded
        categories, which are retained for auditing rather than thrown
        away silently.

    Raises:
        MissingColumnError: If a required column is absent.
        MissingPatchTypeError: If a patch label never appears.
    """
    columns = columns or Columns()
    _validate(frame, columns, pre_label, post_label)

    status = _collapse_runs(frame, columns)
    stable, flaky = _split_flaky(status, columns)
    wide = _pivot_patch_status(stable, columns, pre_label, post_label)

    pre = cast(pd.Series, wide[pre_label])
    post = cast(pd.Series, wide[post_label])

    split = TestSplit(
        fail_to_pass=_to_mapping(
            cast(pd.DataFrame, wide[~pre & post]), columns
        ),
        pass_to_pass=_to_mapping(
            cast(pd.DataFrame, wide[pre & post]), columns
        ),
        regressed=_to_mapping(cast(pd.DataFrame, wide[pre & ~post]), columns),
        broken=_to_mapping(cast(pd.DataFrame, wide[~pre & ~post]), columns),
        flaky=_to_mapping(flaky, columns),
    )

    if split.flaky:
        logger.warning(
            'dropped flaky tests in %d instance(s)',
            len(split.flaky),
        )
    if split.regressed:
        logger.warning(
            'found regressions in %d instance(s); the patch or the '
            'environment is suspect',
            len(split.regressed),
        )
    return split
