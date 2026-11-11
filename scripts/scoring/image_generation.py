"""Keep every instance inside one image generation.

The SWE-bench Multimodal images were rebuilt part-way through this work.
Runs from before and after the rebuild are not interchangeable: for some
instances the new image no longer reproduces the bug at all, so its
before_patch run passes every test. Grading a new-image agent run
against an old-image reference credits the agent for the rebuild, and
grading an old-image agent run against a new reference does the reverse.

So each instance is pinned to a single generation before anything is
scored. The generation chosen is the one that can actually carry a
verdict - a before_patch run that reproduces the bug plus a gold run -
and, among those, the one holding the most agent runs.
"""
import collections
import json
import os

import pandas as pd

REFERENCE_TYPES = ('before_patch', 'gold')

# Runs cluster into campaigns separated by days of silence, and the
# environment moved between them - the images were rebuilt, and the
# later campaigns run a patched harness branch. A gap of two clear
# days is the boundary; the observed gaps are far wider than that.
GAP_DAYS = 2


BOUNDARY_FILE = 'generation_boundaries.json'
_BOUNDS = None

# Campaigns a gap cannot see. The September campaign ran from 17 to 22
# September while twenty evaluator fixes landed underneath it, so "same
# campaign" stopped meaning "same harness": 129 scored rows were graded
# against a before_patch/gold pair made on the other side of a fix to
# their own repo's evaluator. The 23 September round re-ran base, gold
# and every arm for those instances on one commit, the day after the
# last September run - too close for GAP_DAYS to split it off - so it
# is declared a campaign of its own here rather than silently pooled
# with the runs it exists to replace.
#
# The 24 September round is the first whose openlayers runs execute the
# rendering cases (and whose prism gold runs keep CRLF test bytes), so
# its references carry tests no earlier run reported. Grading an older
# run against one would count those tests as missing.
#
# It starts at noon, not midnight: the 23 September round's last wave
# of openlayers before_patch/gold runs (121 of them, no rendering cases)
# ran until 01:30 UTC on the 24th. With the boundary at midnight they
# fell into the new campaign, and choose_run paired old-harness bases
# with new-harness golds - 48 "truncated base" quarantines and a gold
# that failed its own tests. Nothing ran between 02:00 and 12:00.
EXTRA_BOUNDARIES = ['2026-09-23 00:00:00+00:00',
                    '2026-09-24 12:00:00+00:00']

# Runs that are not evaluations. From 19:00 on 22 September to 03:00 on
# 23 September (UTC) the openlayers before_patch regression was bisected
# across harness commits: 557 before_patch and gold runs over all 59
# openlayers instances, about five of each per instance, in four waves.
# Each wave answers "does this commit reproduce it", so a reference cut
# from one of them is built on whichever commit that wave happened to
# test - and being the newest runs, they would otherwise displace every
# openlayers reference. They stay in S3; they are only kept out of
# grading. None of them is an agent run.
EXCLUDED_RUNS = [
    # (instance prefix, patch types, from, until) - until is exclusive
    ('openlayers__', ('before_patch', 'gold'),
     '2026-09-22 19:00:00+00:00', '2026-09-23 03:30:00+00:00'),
    # The four-run check of 319bf5a's quarto fix (4064 and 475), which
    # still ran the whole deno suite. 50c98bb then restricted quarto to
    # each instance's own test files and every quarto run was repeated
    # on it, so these are a superseded harness, not evaluations.
    ('quarto-dev__', ('before_patch', 'gold'),
     '2026-09-23 15:30:00+00:00', '2026-09-23 15:45:00+00:00'),
    # The first rendering canary (a4ba17e: openlayers-13333, -13974,
    # -14332). Every case died at the runner's 60 s page timeout while
    # webpack was still bundling -- the bug 4e35ec4 fixes -- so these
    # measure that timeout, not the cases.
    ('openlayers__', ('before_patch', 'gold'),
     '2026-09-24 12:00:00+00:00', '2026-09-24 13:15:00+00:00'),
    # A second prism-1500 gold run on the same commit as the 13:21
    # canary's. The launcher lost its connection and gave the task up,
    # but the worker finished and uploaded anyway. Kept out so the one
    # reference stays the pair made together at 13:21.
    ('PrismJS__prism-1500', ('gold',),
     '2026-09-24 16:00:00+00:00', '2026-09-24 18:00:00+00:00'),
]


# The 25 September repeat round: a fresh before_patch, gold and both
# image arms, twice over, for each MM>Text pair of the paper's stability
# table (OpenHands-Versa and Refact.ai). It is a repeat of verdicts that
# are already scored, so it must not become the newest run of any cell
# and silently replace them. newpaper3/repeat_round.py grades it on its
# own, with SBMDT_REPEAT_ROUND=1 to lift this exclusion.
REPEAT_ROUND_WINDOW = ('2026-09-25 19:40:00+00:00',
                       '2026-09-26 19:40:00+00:00')
REPEAT_ROUND_INSTANCES = [
    'eslint__eslint-15243', 'prettier__prettier-14262',
    'alibaba-fusion__next-1063', 'bpmn-io__bpmn-js-1557',
    'highlightjs__highlight.js-3018', 'openlayers__openlayers-11226',
    'openlayers__openlayers-12962', 'openlayers__openlayers-15365',
    'quarto-dev__quarto-cli-475']

# The 25 September OCR round: OpenHands-Versa's OCR-condition patches
# (agent llm.claude4-ocr) for the paper's 30 issues lost without images,
# each with a fresh before_patch and gold run because 27 of their
# references are from gen7/gen8. Its before_patch/gold runs would
# otherwise become these instances' newest references and move the main
# tables, so it is kept out the same way and graded on its own by
# newpaper3/ocr_round.py (SBMDT_REPEAT_ROUND=1 lifts this too).
OCR_ROUND_WINDOW = ('2026-09-25 20:30:00+00:00',
                    '2026-09-27 00:00:00+00:00')
OCR_ROUND_INSTANCES = [
    'PrismJS__prism-2195', 'PrismJS__prism-2861', 'PrismJS__prism-3438',
    'alibaba-fusion__next-1067', 'alibaba-fusion__next-2984',
    'alibaba-fusion__next-717', 'alibaba-fusion__next-877',
    'bpmn-io__bpmn-js-1083', 'bpmn-io__bpmn-js-1179',
    'bpmn-io__bpmn-js-1196', 'bpmn-io__bpmn-js-1198',
    'bpmn-io__bpmn-js-1659', 'bpmn-io__bpmn-js-1679',
    'carbon-design-system__carbon-12329',
    'carbon-design-system__carbon-12398',
    'carbon-design-system__carbon-13364',
    'carbon-design-system__carbon-7350',
    'carbon-design-system__carbon-8720',
    'carbon-design-system__carbon-9402', 'eslint__eslint-17618',
    'highlightjs__highlight.js-3018', 'openlayers__openlayers-14414',
    'openlayers__openlayers-14619', 'openlayers__openlayers-15825',
    'prettier__prettier-16347', 'prettier__prettier-8536',
    'quarto-dev__quarto-cli-2689', 'quarto-dev__quarto-cli-2756',
    'quarto-dev__quarto-cli-4064', 'quarto-dev__quarto-cli-4184']


def _rules():
    if os.environ.get('SBMDT_REPEAT_ROUND'):
        return EXCLUDED_RUNS
    types = ('before_patch', 'gold', 'with_image', 'without_image')
    # Matched whole, not as a prefix: openlayers-11226 would otherwise
    # also catch any openlayers-11226x.
    return (EXCLUDED_RUNS
            + [(i, types) + REPEAT_ROUND_WINDOW + (True,)
               for i in REPEAT_ROUND_INSTANCES]
            + [(i, types) + OCR_ROUND_WINDOW + (True,)
               for i in OCR_ROUND_INSTANCES])


def drop_excluded(frame, verbose=True):
    """Remove the runs EXCLUDED_RUNS lists; see there for why."""
    keep = pd.Series(True, index=frame.index)
    for rule in _rules():
        prefix, types, start, end = rule[:4]
        exact = len(rule) > 4 and rule[4]
        name = (frame.instance_id == prefix if exact
                else frame.instance_id.str.startswith(prefix))
        hit = (name
               & frame.patch_type.isin(types)
               & (frame.timestamp >= pd.Timestamp(start))
               & (frame.timestamp < pd.Timestamp(end)))
        keep &= ~hit
    if verbose and (~keep).any():
        print('excluded %d row(s) from runs that are not evaluations'
              % int((~keep).sum()))
    return frame[keep]


def _boundaries(source='all_test_results.parquet'):
    """Campaign start days, derived once from the whole result set.

    The labels have to mean the same thing to every caller, so they are
    computed from all the data and cached. Deriving them from whatever
    subset a caller happens to hold would give two scripts different
    names for the same campaign.
    """
    global _BOUNDS
    if _BOUNDS is not None:
        return _BOUNDS
    extra = [pd.Timestamp(t) for t in EXTRA_BOUNDARIES]
    if os.path.exists(BOUNDARY_FILE):
        with open(BOUNDARY_FILE) as fh:
            cached = [pd.Timestamp(t) for t in json.load(fh)]
            _BOUNDS = sorted(set(cached) | set(extra))
            return _BOUNDS
    days = pd.Index(sorted(set(
        pd.read_parquet(source, columns=['timestamp'])
        .timestamp.dt.normalize())))
    bounds, prev = [], None
    for d in days:
        if prev is None or (d - prev).days > GAP_DAYS:
            bounds.append(d)
        prev = d
    with open(BOUNDARY_FILE, 'w') as fh:
        json.dump([str(t) for t in bounds], fh)
    _BOUNDS = sorted(set(bounds) | set(extra))
    return _BOUNDS


def generation_of(timestamps):
    """Label each timestamp with the campaign it belongs to.

    Compared at full resolution, not by day. The gap-derived boundaries
    are midnights, where the two agree; EXTRA_BOUNDARIES can fall
    mid-day, where only this does (a noon boundary compared by day
    would put the whole day on its far side).
    """
    bounds = pd.DatetimeIndex(_boundaries()).tz_convert('UTC')
    ts = pd.to_datetime(timestamps).dt.tz_convert('UTC')
    idx = bounds.searchsorted(pd.DatetimeIndex(ts), side='right') - 1
    return pd.Series(['gen%d' % i for i in idx], index=ts.index)


def in_reference_campaign(frame, ref):
    """Keep, for each instance, only runs from its reference's campaign.

    The reference records which campaign its before_patch/gold pair came
    from. An agent run from any other campaign was measured in a
    different environment - a rebuilt image, or a harness fix that landed
    in between - and grading it against this reference charges the agent
    for whatever changed. Instances with no reference are left alone, so
    they can still be reported as ungradeable.
    """
    want = frame.instance_id.map(
        {i: e.get('campaign') for i, e in ref.items()})
    gen = generation_of(frame.timestamp)
    keep = want.isna().to_numpy() | (gen.to_numpy() == want.to_numpy())
    return frame[keep]


def run_verdicts(frame):
    """(instance, patch_type, timestamp) -> {test_name: passed}.

    A title can appear more than once in one run -- carbon reports
    'Public API should only change with a semver change' twice, prettier
    'snippet: #0 format' five times -- so a test passes only if every
    copy of it passed, the rule classify_tests applies to the same rows.
    Keeping whichever copy came last instead hid the one failing copy
    that is the whole reproduction for 17 carbon and 4 prettier
    instances, and choose_run then dropped them as non-reproducing.
    """
    book = {}
    verdict = frame.groupby(['instance_id', 'patch_type', 'timestamp',
                             'test_name'], sort=False).passed.all()
    for key, grp in verdict.groupby(level=[0, 1, 2], sort=False):
        book[key] = dict(zip(grp.index.get_level_values(3),
                             grp.to_numpy().tolist(), strict=True))
    return book


# An agent run is a verdict only if it reported at least this share of
# the tests its reference expects - the same 90% bar score_final applies
# before a before_patch or gold run may define a reference. alibaba-4182
# stops at test 117 of ~1,550 on patched and unpatched code alike; grading
# such a run marks every test it never reached as failed and calls the
# agent unresolved for the harness stopping.
COMPLETE = 0.90


def drop_incomplete_runs(frame, ref, verbose=True):
    """Drop agent runs that reported too few of their reference's tests.

    Returns the filtered frame and the set of (instance, patch_type,
    agent_name) cells left with no run at all, which have to be reported
    as unmeasured rather than silently disappearing.
    """
    want = {i: set(e['f2p']) | set(e['p2p']) for i, e in ref.items()}
    agent = (~frame.patch_type.isin(REFERENCE_TYPES)
             & frame.instance_id.isin(want))
    keys = ['instance_id', 'patch_type', 'agent_name', 'timestamp']
    short = set()
    for key, grp in frame[agent].groupby(keys, sort=False):
        expected = want[key[0]]
        if len(expected & set(grp.test_name)) < COMPLETE * len(expected):
            short.add(key)
    if not short:
        return frame, set()
    hit = pd.Series([k in short for k in zip(
        *(frame[c] for c in keys), strict=True)], index=frame.index)
    kept = frame[~hit]
    cells = {k[:3] for k in short}
    left = set(zip(kept.instance_id, kept.patch_type, kept.agent_name,
                   strict=True))
    cut = cells - left
    if verbose:
        print('set aside %d agent run(s) that reported under %d%% of their '
              "reference's tests; %d cell(s) are left with no complete run"
              % (len(short), COMPLETE * 100, len(cut)))
    return kept, cut


def pin_to_one_generation(frame, verbose=True):
    """Drop runs that do not belong to each instance's chosen generation."""
    # Every scorer passes its freshly loaded results through here, so
    # this is the one place that keeps non-evaluation runs out of all
    # of them, whether or not pinning is switched on.
    frame = drop_excluded(frame, verbose=verbose)
    # Off by default. Discarding a whole campaign was the first
    # answer to cross-campaign contamination; grading each run
    # against its own campaign's reference is the better one, and it
    # keeps the runs this would throw away. Kept behind a flag
    # because it is still the sharpest way to ask what the numbers
    # look like on one environment alone.
    if not os.environ.get('SBMDT_PIN'):
        return frame, {}
    gen = generation_of(frame.timestamp)
    runs = (frame.assign(gen=gen)
            .groupby(['instance_id', 'gen', 'patch_type', 'agent_name',
                      'timestamp'])
            .passed.agg(fail=lambda s: int((~s).sum())).reset_index())

    choice, mixed, single = {}, 0, 0
    for inst, sub in runs.groupby('instance_id'):
        best, best_key = None, None
        for g, gs in sub.groupby('gen'):
            repro = not gs[(gs.patch_type == 'before_patch')
                           & (gs.fail > 0)].empty
            gold = not gs[gs.patch_type == 'gold'].empty
            agents = len(gs[~gs.patch_type.isin(REFERENCE_TYPES)])
            key = (1 if (repro and gold) else 0, agents)
            if best_key is None or key > best_key:
                best, best_key = g, key
        if sub.gen.nunique() > 1:
            mixed += 1
        else:
            single += 1
        # Nothing usable anywhere: keep both rather than delete the
        # instance, and let the scorer report it as ungradeable.
        choice[inst] = best if best_key[0] else None

    keep = pd.Series(
        [choice.get(i) in (None, g) for i, g in zip(frame.instance_id, gen, strict=False)],
        index=frame.index)
    if verbose:
        pinned = sum(1 for v in choice.values() if v)
        # This used to name a single cutoff date, `CUT`, and count the
        # two labels 'old' and 'new'. Campaigns are now derived from
        # `_boundaries()` and labelled gen0..genN, so CUT no longer
        # exists - the message raised NameError on the one path that
        # printed it - and the two counts were always zero.
        print('image generations: %d instance(s) have runs in more than '
              'one campaign' % mixed)
        spread = collections.Counter(v for v in choice.values() if v)
        print('  pinned to one campaign: %d  (%s)'
              % (pinned,
                 ', '.join('%s %d' % kv for kv in sorted(spread.items()))
                 or 'none'))
        print('  no generation carries a reference: %d'
              % sum(1 for v in choice.values() if v is None))
        print('  rows dropped as cross-generation: %d of %d'
              % (int((~keep).sum()), len(frame)))
    return frame[keep], choice
