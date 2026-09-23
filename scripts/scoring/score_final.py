"""Build the FAIL_TO_PASS / PASS_TO_PASS reference for the AWS results.

The official lists are redacted in every copy of the benchmark we have, so
the reference is rebuilt from the before_patch and gold runs using the
project's own classifier (notebooks/test_split.py) rather than a private
reimplementation - the deliverables and the harness then agree by
construction.

The classifier anchors FAIL_TO_PASS to the tests each instance's
test_patch.diff names, drops flaky tests, and quarantines instances whose
before_patch run was truncated or whose gold run fails its own tests.
"""
import importlib.util
import json
import os
import re
import sys

import pandas as pd

# The repository checkout these scripts read `notebooks/test_split.py`
# and `dockerfiles/` from. They run from a data directory holding the
# synced results, which is not the repository itself, so the path is
# configurable; the default is the layout used while this was built.
REPO = os.environ.get('SBMDT_REPO', 'swe-bench-m-delta-testing')

spec = importlib.util.spec_from_file_location(
    'test_split', os.path.join(REPO, 'notebooks', 'test_split.py'))
ts = importlib.util.module_from_spec(spec)
sys.modules['test_split'] = ts
spec.loader.exec_module(ts)


def read_test_patches():
    """instance id -> contents of its test_patch.diff."""
    out = {}
    base = os.path.join(REPO, 'dockerfiles')
    for inst in os.listdir(base):
        path = os.path.join(base, inst, 'test_patch.diff')
        if os.path.exists(path):
            out[inst] = open(
                path, encoding='utf-8', errors='replace').read()
    return out


import image_generation as ig

_generation_of = ig.generation_of

raw_big = ig.drop_excluded(pd.read_parquet('all_test_results.parquet'))
big, _generation = ig.pin_to_one_generation(raw_big)
frame = big[big.patch_type.isin(['before_patch', 'gold'])]

# classify_tests deliberately ignores `timestamp` and treats every row for
# an (instance, patch_type, test) as a repeat of one measurement, so it is
# the caller's job to pick which run counts. Pooling a July gold run with a
# September one makes any test whose verdict moved between them disagree
# with itself, and it is then dropped as "flaky" instead of letting the
# current environment define the reference.
#
# Newest-wins is not enough on its own. Two kinds of run carry no
# information about the bug and must not be allowed to displace a run that
# does:
#
#   truncated     the suite died early, so most tests have no verdict at
#                 all. A 16-test run cannot overrule a 12,727-test one.
#   non-reproducing
#                 a before_patch run in which every test passes. The whole
#                 premise of before_patch is that the bug is present, so a
#                 green one is a failed reproduction, not a measurement of
#                 a repo that has no failing test. Seventeen instances on
#                 the paper's base set lost their reference exactly this
#                 way, when a fresh green before_patch run overwrote an
#                 older one that had correctly reproduced the failure.
#
# Both filters only ever discard a run when a better run of the same kind
# exists for the same instance, so nothing is dropped down to nothing.
TRUNCATED = 0.90


# (instance, patch_type, timestamp) -> {test_name: passed}; a title
# repeated within one run passes only if every copy did.
run_tests = ig.run_verdicts


def reproduces(book, inst, base_ts, gold_ts):
    """Does this pair demonstrate the bug at all?

    A before_patch run is only a reference if it fails a test that the
    gold run passes. Failing some unrelated test does not reproduce
    anything; it is how a flaky or half-broken run sneaks in and then
    yields an empty FAIL_TO_PASS list.
    """
    base = book.get((inst, 'before_patch', base_ts), {})
    gold = book.get((inst, 'gold', gold_ts), {})
    return any(gold.get(t) is True for t, ok in base.items() if not ok)


def choose_run(frame):
    """One timestamp per (instance, patch_type).

    For before_patch and gold the pair is chosen together: the most
    recent pair of full-coverage runs that actually reproduces the bug.
    When the newest pair already reproduces - the ordinary case - this
    is exactly the newest run of each, so only broken instances move.
    """
    gen = _generation_of(frame.timestamp)
    frame = frame.assign(_gen=gen)
    stat = (frame.groupby(['instance_id', 'patch_type', 'timestamp',
                           '_gen'])
            .agg(n_tests=('test_name', 'nunique'),
                 n_failed=('passed', lambda s: int((~s).sum())))
            .reset_index())
    widest = stat.groupby('instance_id').n_tests.transform('max')
    stat['full'] = stat.n_tests >= TRUNCATED * widest
    stat['reproduced'] = (stat.patch_type != 'before_patch') |         (stat.n_failed > 0)

    book = run_tests(frame)
    keep, dropped = [], {'truncated': 0, 'non_reproducing': 0,
                         'repaired': 0, 'cross_campaign': 0}

    def newest(pool):
        return pool.loc[pool.timestamp.idxmax()]

    for inst, ig in stat.groupby('instance_id'):
        picked = {}
        for pt, grp in ig.groupby('patch_type'):
            pool = grp[grp.full]
            if pool.empty:
                pool = grp
            elif len(pool) < len(grp):
                dropped['truncated'] += len(grp) - len(pool)
            alive = pool[pool.reproduced]
            if not alive.empty:
                dropped['non_reproducing'] += len(pool) - len(alive)
                pool = alive
            picked[pt] = (newest(pool).timestamp, pool)

        # The pair carries the reference, so repair it as a pair.
        if 'before_patch' in picked and 'gold' in picked:
            b_ts, b_pool = picked['before_patch']
            g_ts, g_pool = picked['gold']
            # Same campaign only. A base from one side of the image
            # rebuild and a gold from the other differ by whole
            # blocks of tests, and every test the gold ran that the
            # base never did reads as FAIL_TO_PASS.
            same_gen = (
                set(b_pool[b_pool.timestamp == b_ts]._gen)
                == set(g_pool[g_pool.timestamp == g_ts]._gen)
            )
            if not same_gen or not reproduces(book, inst, b_ts, g_ts):
                cand = [(max(str(b), str(g)), str(b), b, g)
                        for b in b_pool.timestamp for g in g_pool.timestamp
                        if set(b_pool[b_pool.timestamp == b]._gen)
                        == set(g_pool[g_pool.timestamp == g]._gen)
                        and reproduces(book, inst, b, g)]
                if cand:
                    cand.sort(reverse=True)
                    picked['before_patch'] = (cand[0][2], b_pool)
                    picked['gold'] = (cand[0][3], g_pool)
                    dropped['repaired'] += 1
                else:
                    # Nothing gradeable: drop the instance rather
                    # than keep a pair that spans the rebuild.
                    dropped['cross_campaign'] += 1
                    continue

        for pt, (ts, _) in picked.items():
            keep.append((inst, pt, ts))
    return set(keep), dropped, stat


chosen, dropped, stat = choose_run(frame)
stale = int(stat.groupby(['instance_id', 'patch_type']).size().gt(1).sum())
frame = frame[[(a, b, c) in chosen for a, b, c in
               zip(frame.instance_id, frame.patch_type, frame.timestamp, strict=False)]]
print('reference runs: one per instance/patch_type '
      '(%d cell(s) had more than one run)' % stale)
print('  passed over: %d truncated, %d non-reproducing before_patch'
      % (dropped['truncated'], dropped['non_reproducing']))
print('  %d instance(s) had their before_patch/gold pair repaired: the '
      'newest runs did not reproduce the bug together'
      % dropped['repaired'])
print('  %d instance(s) dropped: no before_patch/gold pair from one '
      'campaign reproduces the bug' % dropped['cross_campaign'])

diffs = read_test_patches()

# Test suites the harness never executes. The openlayers evaluator runs
# only the karma suite; test/rendering/ is a separate pixel-diff runner
# whose expected PNGs the released diffs do not even carry. When an
# instance's test patch touches nothing else, the bug's own tests never
# run, and whatever flips in the unit suite between base and gold is
# unrelated -- openlayers-13013's gold patch changes only WebGL tile
# textures, yet its delta was 'ol/View fit animates when duration is
# defined'. Such an instance has no reference to grade against.
UNRUN_TEST_PATHS = {'openlayers': ('test/rendering/', 'rendering/')}


def tests_never_run(inst):
    prefixes = UNRUN_TEST_PATHS.get(inst.split('__')[0])
    if not prefixes:
        return False
    files = re.findall(r'^diff --git a/(\S+)', diffs.get(inst, ''), re.M)
    return bool(files) and all(f.startswith(prefixes) for f in files)

# `timestamp` tells the classifier which rows came from the same run.
# Without it a test title that appears more than once in one suite --
# prettier reports 'snippet: #0 format' five times -- looks like one
# test that disagreed with itself, and gets dropped as flaky.
COLUMNS = ts.Columns(run='timestamp')

split = ts.classify_tests(
    frame,
    pre_label='before_patch',
    post_label='gold',
    columns=COLUMNS,
    test_patch_diffs=diffs,
)

# Tests seen to give different verdicts on byte-identical code
# (flaky_tests.py). A FAIL_TO_PASS or PASS_TO_PASS entry that can flip with
# no change to the code is not a requirement a patch can be held to, so it
# is dropped for every agent alike. Absent file, nothing is dropped.
try:
    _flaky = pd.read_csv('flaky_tests.csv')
    FLAKY = {(r.repo, r.test) for r in _flaky.itertuples()}
except (OSError, pd.errors.EmptyDataError):
    FLAKY = set()


def unflaky(inst, tests):
    repo = inst.split('__')[0]
    return [t for t in tests if (repo, t) not in FLAKY]


def tier_of(inst):
    """How the instance's FAIL_TO_PASS list was arrived at."""
    leaf, block = ts.test_patch_titles(diffs.get(inst, ''))
    if leaf:
        return 'anchored'
    if block:
        return 'anchored-block'
    return 'derived'


# An instance is only gradeable when it has at least one FAIL_TO_PASS test:
# with none, every patch trivially "passes" and the score is meaningless.
# Which campaign each reference's pair came from, so a run can be graded
# only against a reference measured in its own environment.
_gold_ts = pd.Series({i: t for i, pt, t in chosen if pt == 'gold'})
campaign_of = dict(zip(_gold_ts.index, _generation_of(_gold_ts)))
ref = {
    inst: {'f2p': unflaky(inst, f2p),
           'p2p': unflaky(inst, split.pass_to_pass.get(inst, [])),
           'tier': tier_of(inst),
           'campaign': campaign_of.get(inst)}
    for inst, f2p in split.fail_to_pass.items()
    if unflaky(inst, f2p) and not tests_never_run(inst)
}
unrun = sorted(i for i in split.fail_to_pass if tests_never_run(i))
print('instances whose only tests are in a suite the harness never runs: '
      '%d (%s)' % (len(unrun), ', '.join(unrun) or 'none'))
emptied = sorted(i for i, f in split.fail_to_pass.items() if not unflaky(i, f))
print('flaky tests dropped from references: %d test name(s); %d instance(s) '
      'left with no FAIL_TO_PASS and so ungradeable' % (len(FLAKY), len(emptied)))

sizes = sorted((len(v['f2p']) for v in ref.values()), reverse=True)
print('gradeable instances : %d' % len(ref))
print('  F2P size median=%d  max=%d  >20 tests=%d'
      % (sizes[len(sizes) // 2], sizes[0], sum(1 for s in sizes if s > 20)))
tiers = pd.Series([v['tier'] for v in ref.values()]).value_counts()
for name, n in tiers.items():
    print('  %-16s  : %d' % (name, n))
print('quarantined         : %d' % len(split.incomplete))
print('flaky tests dropped : %d instance(s)' % len(split.flaky))

with open('reference_final.json', 'w') as f:
    json.dump({'ref': ref,
               'flaky_emptied': emptied,
               'tests_never_run': unrun,
               'quarantined': [{'instance_id': i, 'dropped_tests': len(t)}
                               for i, t in sorted(split.incomplete.items())],
               'flaky': {i: len(t) for i, t in split.flaky.items()}}, f)
print('\nwrote reference_final.json')


# ---------------------------------------------------------------------
# A second reference, cut per campaign.
#
# Runs cluster into campaigns separated by days of silence, and the
# environment moved between them: the images were rebuilt and the
# harness itself gained fixes (a cancelled OpenLayers suite, ESLint's
# Mocha flags). A before_patch run from one campaign and an agent run
# from the next are not a controlled comparison - whatever changed in
# between is charged to the agent.
#
# Rather than throw one campaign away, every campaign gets its own
# reference, and score_agents grades each run against the reference
# from the campaign it was measured in. An agent run whose own campaign
# never produced a usable before_patch/gold pair is simply not graded,
# which is the honest outcome: on that environment the instance has no
# reference to grade against.
by_gen = {}
_gen_all = ig.generation_of(raw_big.timestamp)
for _g in sorted(set(_gen_all.dropna())):
    _sub = raw_big[_gen_all == _g]
    _fr = _sub[_sub.patch_type.isin(['before_patch', 'gold'])]
    # A campaign that ran only one side of the pair cannot define a
    # reference, and classify_tests refuses it outright.
    if set(_fr.patch_type.unique()) != {'before_patch', 'gold'}:
        continue
    _chosen, _, _ = choose_run(_fr)
    _fr = _fr[[(a, b, c) in _chosen for a, b, c in
               zip(_fr.instance_id, _fr.patch_type, _fr.timestamp, strict=False)]]
    # Requiring each pair to reproduce the bug can leave a campaign
    # with before_patch runs and no gold at all (gen3 and gen4: every
    # gold there matches its base). Nothing in it can be a reference.
    if set(_fr.patch_type.unique()) != {'before_patch', 'gold'}:
        continue
    _split = ts.classify_tests(_fr, pre_label='before_patch',
                               post_label='gold', columns=COLUMNS,
                               test_patch_diffs=diffs)
    for _inst, _f2p in _split.fail_to_pass.items():
        if not unflaky(_inst, _f2p) or tests_never_run(_inst):
            continue
        by_gen[f'{_inst}|{_g}'] = {
            'f2p': unflaky(_inst, _f2p),
            'p2p': unflaky(_inst, _split.pass_to_pass.get(_inst, [])),
            'tier': tier_of(_inst),
            'campaign': _g}

with open('reference_by_generation.json', 'w') as f:
    json.dump(by_gen, f)
_covered = {k.split('|')[0] for k in by_gen}
print('per-campaign references: %d (instance, campaign) pair(s) across '
      '%d instance(s)' % (len(by_gen), len(_covered)))
print('wrote reference_by_generation.json')
