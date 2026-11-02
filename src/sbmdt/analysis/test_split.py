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
import re
from collections.abc import Mapping
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

# A test absent from the pre-patch run is treated as FAIL_TO_PASS, which is
# correct when the patch introduced it but catastrophic when the pre-patch
# run simply died partway through the suite: every test it never reached
# then looks introduced. Observed in practice -- one instance's pre-patch
# run covered 34 of 1202 tests and produced 1168 bogus FAIL_TO_PASS entries,
# against a real reference list of one or two.
#
# Real test patches are tiny relative to the suite: across this corpus the
# median patch adds 2 tests (0.14% of the suite) and the 75th percentile
# adds 9 (0.46%). The truncated runs sit far away at 27-97%. Requiring the
# pre-patch run to cover at least this fraction of the post-patch run
# separates the two cleanly, with roughly an order of magnitude of slack on
# either side.
MIN_PRE_RUN_COVERAGE: float = 0.95

# Truncation is not the only way to inflate a FAIL_TO_PASS list. Renaming a
# suite -- an outer describe() block, say -- changes the full name of every
# test beneath it, so all of them read as introduced even though the
# pre-patch run completed normally. The coverage guard cannot see this, and
# no threshold distinguishes it from a genuinely large patch, so instances
# above this size are reported for inspection rather than dropped. Real
# SWE-bench reference lists are typically one to twenty tests.
IMPLAUSIBLE_F2P_COUNT: int = 20

#: A test title matching more than this many tests is generic ("renders",
#: "should render") and cannot pin a single test on its own, so the
#: pre/post delta is used to decide which of its matches actually changed.
AMBIGUOUS_TITLE_MATCHES: int = 3

#: Added `it`/`test`/`describe` titles in a test patch. The quote character
#: is back-referenced so apostrophes inside a double-quoted title survive.
TEST_TITLE_PATTERN = re.compile(
    r"""^\+\s*(it|test|describe)(?:\.\w+)?\s*\(\s*(['"`])(.+?)\2""",
    re.MULTILINE,
)

#: Prefix of the test name the evaluator reports a rendering case under.
RENDERING_TITLE_PREFIX = 'rendering '

#: A rendering case the test patch adds or changes (openlayers).
RENDERING_CASE_PATTERN = re.compile(
    r'^diff --git a/\S+ b/test/rendering/cases/([^/\s]+)/', re.MULTILINE
)

#: Titles shorter than this, or carrying an interpolation marker, never
#: match a concrete reported test name.
_MIN_TITLE_LENGTH: int = 6
_TEMPLATE_MARKERS: tuple[str, ...] = ('${', '%s', '%d')

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
    # Which column separates one execution of the suite from another.
    # Without it a test name that occurs twice *inside a single run* is
    # indistinguishable from one test that answered differently on two
    # different days, and the first gets thrown away as flaky. Optional
    # so existing callers keep their current behaviour.
    run: str | None = None

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
        incomplete: Instances whose pre-patch run covered too little of
            the post-patch suite to be trusted (see
            `MIN_PRE_RUN_COVERAGE`), or whose post-patch run failed the
            tests its own test patch adds. Their tests are excluded from every
            other set, because a truncated pre-patch run manufactures
            FAIL_TO_PASS entries out of tests it never reached. The value
            is the post-patch test list, kept for auditing.
    """

    fail_to_pass: dict[str, list[str]]
    pass_to_pass: dict[str, list[str]]
    regressed: dict[str, list[str]]
    broken: dict[str, list[str]]
    flaky: dict[str, list[str]]
    incomplete: dict[str, list[str]]


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
    frame = cast(pd.DataFrame, frame.assign(**{columns.passed: passed}))

    # A test name is not a unique test. jest and mocha both report the
    # bare title, so the same title in two files arrives as two rows of
    # one run -- prettier-8536 reports 'snippet: #0 format' five times,
    # carbon-10214 reports 'Public API should only change with a semver
    # change' twice. When one of those rows fails and the others pass,
    # the all/any disagreement that is meant to detect a flaky test
    # fires on a name collision instead, and the name is dropped from
    # the reference entirely. That cost 17 instances their whole
    # FAIL_TO_PASS list and pulled 144 tests out of 91 instances.
    #
    # Collapsing within a run first fixes it: duplicates of a name in
    # one run become a single verdict under the same all-must-pass rule
    # applied everywhere else, so disagreement can then only mean what
    # it is supposed to mean -- two runs that answered differently.
    if columns.run is not None and columns.run in frame.columns:
        frame = cast(
            pd.DataFrame,
            frame.groupby(keys + [columns.run], dropna=False, observed=True)[
                columns.passed
            ]
            .all()
            .reset_index(),
        )

    return cast(
        pd.DataFrame,
        (
            frame
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


def _find_incomplete_pre_runs(
    status: pd.DataFrame,
    columns: Columns,
    pre_label: str,
    post_label: str,
    min_coverage: float,
) -> set[str]:
    """Identify instances whose pre-patch run is too short to trust.

    Compares how many distinct tests each side ran. A pre-patch run that
    covered far fewer tests than the post-patch run did not observe the
    suite, so the tests it never reached would be misread as introduced
    by the patch.

    Args:
        status: Output of `_collapse_runs`.
        columns: The column-name mapping.
        pre_label: Value of the patch_type column for pre-patch runs.
        post_label: Value of the patch_type column for post-patch runs.
        min_coverage: Minimum pre/post test-count ratio to accept.

    Returns:
        The instance ids to quarantine. Instances missing either side
        entirely are included, since no comparison is possible.
    """
    counts = (
        status.groupby([columns.instance, columns.patch_type], observed=True)[
            columns.test_name
        ]
        .nunique()
        .unstack(columns.patch_type)
        .reindex(columns=[pre_label, post_label])
    )
    pre = counts[pre_label].fillna(0)
    post = counts[post_label].fillna(0)
    # An instance with no post-patch run is dropped later by
    # _pivot_patch_status anyway; guard the division rather than flag it
    # here, so this function only reports genuine truncation.
    coverage = pre.where(post > 0) / post.where(post > 0)
    return set(coverage.index[coverage < min_coverage])


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



def test_patch_titles(diff: str) -> tuple[list[str], list[str]]:
    """Split the titles a test patch adds into leaf tests and blocks.

    An `it`/`test` title names one test and can be matched directly. A
    `describe` title names a whole block, so on its own it would pull in
    every pre-existing test under that block; it is only usable together
    with the pre/post delta.

    Args:
        diff: Contents of the instance's test_patch.diff.

    Returns:
        A (leaf, block) pair of de-duplicated title lists.
    """
    leaf: list[str] = []
    block: list[str] = []
    seen: set[str] = set()
    for match in TEST_TITLE_PATTERN.finditer(diff):
        kind, title = match.group(1), match.group(3).strip()
        if len(title) < _MIN_TITLE_LENGTH or title in seen:
            continue
        if any(marker in title for marker in _TEMPLATE_MARKERS):
            continue
        seen.add(title)
        (block if kind == 'describe' else leaf).append(title)
    # An openlayers rendering case is a directory, not an it() title;
    # the evaluator reports it as 'rendering <case>'.
    for case in RENDERING_CASE_PATTERN.findall(diff):
        title = f'{RENDERING_TITLE_PREFIX}{case}'
        if title not in seen:
            seen.add(title)
            leaf.append(title)
    return leaf, block


def _anchored_fail_to_pass(
    wide: pd.DataFrame,
    columns: Columns,
    pre_label: str,
    post_label: str,
    test_patch_diffs: Mapping[str, str],
) -> dict[str, set[str]]:
    """Pin FAIL_TO_PASS to the tests each instance's test patch names.

    The delta rule alone cannot tell a newly added test from one the patch
    merely renamed or reordered, which is what inflates a reference to
    hundreds of tests. The test patch names what it added, so prefer it.

    Args:
        wide: One row per (instance, test) with boolean pre/post columns.
        columns: The column-name mapping.
        pre_label: Column holding the pre-patch verdict.
        post_label: Column holding the post-patch verdict.
        test_patch_diffs: Instance id to test_patch.diff contents.

    Returns:
        A (anchored, unsatisfied) pair. `anchored` maps instance id to the
        anchored FAIL_TO_PASS test names. `unsatisfied` holds instances
        whose patch named tests that the post-patch run then failed, which
        means the reference patch does not satisfy its own tests and the
        instance cannot be graded at all. Instances whose patch named
        nothing recognisable appear in neither, leaving them to the delta
        rule.
    """
    anchored: dict[str, set[str]] = {}
    unsatisfied: set[str] = set()
    post_ok = wide[post_label].astype(bool)
    for key, all_rows in wide.groupby(columns.instance, observed=True):
        instance = str(key)
        diff = test_patch_diffs.get(instance)
        if not diff:
            continue
        leaf, block = test_patch_titles(diff)
        if not leaf and not block:
            continue
        rows = cast(pd.DataFrame, all_rows[post_ok.loc[all_rows.index]])
        names = cast(pd.Series, rows[columns.test_name]).astype(str)
        delta = set(names[~rows[pre_label].astype(bool)])
        hits: set[str] = set()
        for title in leaf:
            matched = {name for name in names if title in name}
            # A specific title names its test outright; a generic one needs
            # the pre-patch run to say which match was actually failing.
            if len(matched) <= AMBIGUOUS_TITLE_MATCHES:
                hits |= matched
            else:
                hits |= matched & delta
        if not hits and block:
            hits = {name for name in delta
                    if any(title in name for title in block)}
        if hits and not hits & delta:
            # Every test the patch names already passes without the fix,
            # so on its own the anchored list is satisfied by an empty
            # patch. The patch did name them, but what it actually made
            # fail is elsewhere -- carbon-12420's new TimePicker test
            # passes unpatched, and only its Public API snapshot moves.
            # Leave the instance to the delta rule, which yields exactly
            # the tests that do fail before the fix, or none at all.
            logger.info(
                '%s: every test its patch names passes before the fix; '
                'using the pre/post delta instead', instance)
            continue
        if hits:
            anchored[instance] = hits
            continue
        # The patch named tests but none of them passed after the patch.
        # If those tests ran at all, the reference itself is broken, so the
        # instance is not gradeable; falling back to the delta rule here is
        # what turns a renamed suite into hundreds of phantom entries.
        # A rendering case is exempt: it is a pixel comparison, and a
        # gold image that differs from this environment's software
        # renderer says nothing about the patch's unit tests.
        every = cast(pd.Series, all_rows[columns.test_name]).astype(str)
        named = [t for t in (*leaf, *block)
                 if not t.startswith(RENDERING_TITLE_PREFIX)]
        if any(title in name for name in every for title in named):
            unsatisfied.add(instance)
    return anchored, unsatisfied


def classify_tests(
    frame: pd.DataFrame,
    pre_label: str,
    post_label: str,
    columns: Columns | None = None,
    min_pre_run_coverage: float = MIN_PRE_RUN_COVERAGE,
    test_patch_diffs: Mapping[str, str] | None = None,
) -> TestSplit:
    """Build FAIL_TO_PASS and PASS_TO_PASS test sets per instance.

    Args:
        frame: Long-format test results, one row per test execution.
        pre_label: Value of the patch_type column marking pre-patch
            runs (the buggy state).
        post_label: Value of the patch_type column marking post-patch
            runs (the fixed state).
        columns: Column-name mapping. Defaults to the standard schema.
        min_pre_run_coverage: Minimum share of the post-patch suite the
            pre-patch run must cover for the instance to be classified
            at all. Set to 0 to disable the guard and restore the old
            behaviour.
        test_patch_diffs: Optional mapping of instance id to the
            contents of that instance's test_patch.diff. When given,
            FAIL_TO_PASS is pinned to the tests the patch names
            instead of being inferred from the pre/post delta alone,
            which is what keeps a renamed suite from becoming a
            reference of hundreds of tests.

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
    incomplete_ids = _find_incomplete_pre_runs(
        status, columns, pre_label, post_label, min_pre_run_coverage
    )
    stable, flaky = _split_flaky(status, columns)
    wide = _pivot_patch_status(stable, columns, pre_label, post_label)

    # Split the quarantined instances off before classifying, so a
    # truncated pre-patch run cannot contribute phantom FAIL_TO_PASS
    # entries to the reference.
    quarantined = cast(pd.Series, wide[columns.instance]).isin(incomplete_ids)
    incomplete_rows = cast(pd.DataFrame, wide[quarantined])
    wide = cast(pd.DataFrame, wide[~quarantined])

    pre = cast(pd.Series, wide[pre_label])
    post = cast(pd.Series, wide[post_label])

    # Default to the delta rule, then let the test patch overrule it for
    # the instances whose patch actually names the tests it adds.
    is_f2p = ~pre & post
    if test_patch_diffs:
        anchored, unsatisfied = _anchored_fail_to_pass(
            wide, columns, pre_label, post_label, test_patch_diffs
        )
        if unsatisfied:
            logger.warning(
                'quarantined %d instance(s) whose %s run failed the tests '
                'their own test patch adds; the reference patch does not '
                'satisfy its own tests, so nothing can be scored against '
                'it: %s',
                len(unsatisfied),
                post_label,
                ', '.join(sorted(unsatisfied)[:5]),
            )
            drop = cast(pd.Series, wide[columns.instance]).astype(
                str).isin(unsatisfied)
            incomplete_rows = pd.concat(
                [incomplete_rows, cast(pd.DataFrame, wide[drop])]
            )
            wide = cast(pd.DataFrame, wide[~drop])
            pre = cast(pd.Series, wide[pre_label])
            post = cast(pd.Series, wide[post_label])
            is_f2p = ~pre & post
        if anchored:
            instances = cast(pd.Series, wide[columns.instance]).astype(str)
            names = cast(pd.Series, wide[columns.test_name]).astype(str)
            picked = pd.Series(
                [name in anchored.get(instance, ())
                 for instance, name in zip(instances, names, strict=True)],
                index=wide.index,
            )
            covered = instances.isin(anchored)
            is_f2p = (~covered & is_f2p) | (covered & post & picked)
            logger.info(
                'anchored FAIL_TO_PASS to the test patch for %d instance(s)',
                len(anchored),
            )

    split = TestSplit(
        fail_to_pass=_to_mapping(cast(pd.DataFrame, wide[is_f2p]), columns),
        pass_to_pass=_to_mapping(
            cast(pd.DataFrame, wide[pre & post & ~is_f2p]), columns
        ),
        regressed=_to_mapping(cast(pd.DataFrame, wide[pre & ~post]), columns),
        broken=_to_mapping(cast(pd.DataFrame, wide[~pre & ~post]), columns),
        flaky=_to_mapping(flaky, columns),
        incomplete=_to_mapping(incomplete_rows, columns),
    )

    if split.incomplete:
        logger.warning(
            'quarantined %d instance(s) whose %s run covered less than '
            '%.0f%% of the %s suite; a truncated run would otherwise '
            'manufacture FAIL_TO_PASS entries',
            len(split.incomplete),
            pre_label,
            min_pre_run_coverage * 100,
            post_label,
        )
    oversized = sorted(
        (
            (len(tests), instance)
            for instance, tests in split.fail_to_pass.items()
            if len(tests) > IMPLAUSIBLE_F2P_COUNT
        ),
        reverse=True,
    )
    if oversized:
        logger.warning(
            'derived a FAIL_TO_PASS list larger than %d tests for %d '
            'instance(s); the patch most likely renamed a suite rather '
            'than adding that many tests, so these references are '
            'unreliable: %s',
            IMPLAUSIBLE_F2P_COUNT,
            len(oversized),
            ', '.join(f'{name} ({count})' for count, name in oversized[:5]),
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
