"""Score every agent run against the reference built by score_final.py.

An instance is RESOLVED when every FAIL_TO_PASS test passes and no PASS_TO_PASS
test is lost.  A test that never ran counts as a failure, which is the same
convention the benchmark's own harness uses.
"""
import json

import pandas as pd

AGENTS = {
    'llm.claude4': 'llm.claude4',
    'refact': 'refact',
    'GUIRepair-o3-2025-04-16-with_image': 'GUIRepair-o3-2025-04-16',
    'GUIRepair-o3-2025-04-16-without_image': 'GUIRepair-o3-2025-04-16',
    # The paper's third RQ1 arm: the instances lost without images, tried
    # again with them. It is a separate run of the same agent, so it has
    # to be a separate name here - sharing llm.claude4 would put two runs
    # under one (instance, patch_type, agent) key and the latest-timestamp
    # rule would drop the paper's own base run in favour of this one.
    'llm.claude4-reattempt': 'llm.claude4-reattempt',
}

ref = json.load(open('reference_final.json'))['ref']
# The same reference, cut per campaign, so a run can be graded against
# the environment it was actually measured in.
by_gen = json.load(open('reference_by_generation.json'))


def environment_drift(runs, reference):
    """Reference tests that fail in every independent agent run.

    The reference is built from the gold run, which may have executed in a
    different environment than the agent runs - the images are tagged
    :latest, so a dependency can change underneath the benchmark. A test
    that gold passes but that every agent fails, in runs made separately
    and by different agents, is far more likely to be that drift than
    three agents independently breaking the same test. Counting it against
    them makes an instance unresolvable by anyone.

    This applies to FAIL_TO_PASS as much as to PASS_TO_PASS. On the paper's
    OpenHands base set, 108 of the 128 FAIL_TO_PASS tests our run misses on
    instances the paper reports as resolved are missed by OpenHands,
    GUIRepair and Refact alike, while gold passes them - three agents that
    share no code failing the identical test is the image, not the patch.
    Leaving F2P out of the rule meant reporting a drift-corrected number
    that was still uncorrected for the larger of the two effects.

    An F2P test only counts as drift when it ran in every independent
    cell, so a test one agent simply never executed cannot be written off
    on the strength of the others.

    Returns:
        instance id -> {'p2p': set, 'f2p': set} of tests no run passed.
    """
    drift = {}
    for inst, sub in runs.groupby('instance_id'):
        entry = reference.get(inst)
        if entry is None:
            continue
        n_runs = sub.groupby(
            ['agent_name', 'patch_type', 'timestamp']).ngroups
        if n_runs < 2:
            continue
        dead = {}
        for kind in ('p2p', 'f2p'):
            want = set(entry[kind])
            seen = sub[sub.test_name.isin(want)]
            if seen.empty:
                dead[kind] = set()
                continue
            # A test must have run everywhere to be judged dead everywhere.
            per_run = seen.groupby(
                ['test_name', 'agent_name', 'patch_type',
                 'timestamp']).passed.all()
            complete = per_run.unstack(level=[1, 2, 3]).dropna()
            dead[kind] = {t for t in complete.index
                          if not complete.loc[t].any()}
        if dead['p2p'] or dead['f2p']:
            drift[inst] = dead
    return drift


import image_generation as ig

big = pd.read_parquet('all_test_results.parquet')
big, _generation = ig.pin_to_one_generation(big)
latest = big.groupby(
    ['instance_id', 'patch_type', 'agent_name'])['timestamp'].transform('max')
L = big[big.timestamp == latest]
runs = L[L.patch_type.isin(['with_image', 'without_image'])
         & L.agent_name.isin(AGENTS)]
runs = runs.assign(campaign=ig.generation_of(runs.timestamp))

# Drift is judged on the three independent agents only. The re-attempt
# arm is the same agent as llm.claude4 and covers 33 instances, so it is
# not independent evidence; worse, a test it never executed would drop
# out of the "ran everywhere" filter and hide drift that is really there.
drift = environment_drift(
    runs[runs.agent_name != 'llm.claude4-reattempt'], ref)
n_p2p_drift = sum(len(v['p2p']) for v in drift.values())
n_f2p_drift = sum(len(v['f2p']) for v in drift.values())

rows = []
for (inst, ptype, raw_agent), sub in runs.groupby(
        ['instance_id', 'patch_type', 'agent_name']):
    entry = ref.get(inst)
    if entry is None:
        rows.append({'instance_id': inst, 'repo': inst.split('__')[0],
                     'agent': AGENTS[raw_agent], 'condition': ptype,
                     'status': 'NO_REFERENCE', 'resolved': pd.NA,
                     'f2p_total': pd.NA, 'f2p_passed': pd.NA,
                     'p2p_total': pd.NA, 'p2p_lost': pd.NA,
                     'tier': pd.NA, 'tests_run': sub.test_name.nunique(),
                     'p2p_lost_excl_drift': pd.NA,
                     'resolved_excl_drift': pd.NA, 'drift_tests': pd.NA,
                     'f2p_drift': pd.NA, 'campaign': pd.NA,
                     'resolved_campaign': pd.NA,
                     'f2p_total_campaign': pd.NA})
        continue
    status = sub.groupby('test_name')['passed'].all().to_dict()
    campaign = sub.campaign.iloc[0]
    same = by_gen.get(f'{inst}|{campaign}')
    if same is None:
        resolved_campaign = pd.NA
        f2p_campaign = pd.NA
    else:
        cf2p, cp2p = same['f2p'], same['p2p']
        resolved_campaign = (
            all(status.get(t) is True for t in cf2p)
            and all(status.get(t) is True for t in cp2p))
        f2p_campaign = len(cf2p)
    f2p, p2p = entry['f2p'], entry['p2p']
    f2p_passed = sum(1 for t in f2p if status.get(t) is True)
    p2p_lost = sum(1 for t in p2p if status.get(t) is not True)
    resolved = f2p_passed == len(f2p) and p2p_lost == 0
    # Same verdict, ignoring tests the environment broke for everyone.
    dead = drift.get(inst, {'p2p': set(), 'f2p': set()})
    p2p_lost_adj = sum(
        1 for t in p2p if t not in dead['p2p'] and status.get(t) is not True)
    live_f2p = [t for t in f2p if t not in dead['f2p']]
    if live_f2p:
        resolved_adj = (all(status.get(t) is True for t in live_f2p)
                        and p2p_lost_adj == 0)
    else:
        # Every FAIL_TO_PASS test is dead for every agent, so this image
        # cannot demonstrate the fix at all. Better to say so than to
        # declare the instance resolved on an empty requirement.
        resolved_adj = pd.NA
    rows.append({'instance_id': inst, 'repo': inst.split('__')[0],
                 'agent': AGENTS[raw_agent], 'condition': ptype,
                 'status': 'RESOLVED' if resolved else 'UNRESOLVED',
                 'resolved': resolved,
                 'f2p_total': len(f2p), 'f2p_passed': f2p_passed,
                 'p2p_total': len(p2p), 'p2p_lost': p2p_lost,
                 'tier': entry['tier'], 'tests_run': sub.test_name.nunique(),
                 'p2p_lost_excl_drift': p2p_lost_adj,
                 'resolved_excl_drift': resolved_adj,
                 'drift_tests': len(dead['p2p']),
                 'f2p_drift': len(dead['f2p']),
                 'campaign': campaign,
                 'resolved_campaign': resolved_campaign,
                 'f2p_total_campaign': f2p_campaign})

df = pd.DataFrame(rows).sort_values(['agent', 'condition', 'instance_id'])
df.to_csv('scored_final.csv', index=False)

print('=== Gradeable runs (instance has a usable reference) ===\n')
g = df[df.status != 'NO_REFERENCE']
for agent in sorted(g.agent.unique()):
    a = g[g.agent == agent]
    print(f'{agent}')
    for cond in ['with_image', 'without_image']:
        c = a[a.condition == cond]
        if len(c) == 0:
            print(f'   {cond:14s}  no runs')
            continue
        n, r = len(c), int(c.resolved.sum())
        ra = int(c.resolved_excl_drift.sum())
        print(f'   {cond:14s}  resolved {r:3d} / {n:3d}  ({r / n:5.1%})'
              f'   excl. env drift {ra:3d}  ({ra / n:5.1%})')
    print()

print('=== Runs dropped for lack of a reference ===')
nr = df[df.status == 'NO_REFERENCE'].groupby(['agent', 'condition']).size()
print(nr.to_string() if len(nr) else '  none')
print(f'\nenvironment drift: {n_p2p_drift} PASS_TO_PASS and '
      f'{n_f2p_drift} FAIL_TO_PASS test(s) across {len(drift)} '
      f'instance(s) failed in EVERY agent run while gold passes them '
      f'- the image moving, not the agents.')
print(f'\nwrote scored_final.csv  ({len(df)} run rows)')
