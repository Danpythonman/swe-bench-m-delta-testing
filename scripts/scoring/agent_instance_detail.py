"""One detailed row per (instance, condition) for every agent.

The existing instance_status_*.csv says *which bucket* an instance fell
into; this says *why*, with the test counts the verdict was computed
from. Every one of the 510 benchmark instances appears for every agent,
including the ones with no prediction - an instance the agent never
attempted is part of its situation, not an absence from the report.

The verdict logic is the one in score_agents.py, recomputed here rather
than joined, because the per-test breakdown (kept / regressed / never
ran) is not in scored_final.csv and cannot be reconstructed from it.

Three states that the first version of this report collapsed into "no
result" are now kept apart, because they call for different actions:

  failed    the run happened and died, and the worker log says how
  no tests  the run uploaded a result file containing zero tests
  no result a patch exists, nothing ran, and nothing explains it
"""
import collections
import json
import re
import urllib.parse

import image_generation as ig
import pandas as pd

AGENTS = {
    'llm.claude4': 'OpenHands-Versa',
    'GUIRepair-o3-2025-04-16-with_image': 'GUIRepair-o3',
    'GUIRepair-o3-2025-04-16-without_image': 'GUIRepair-o3',
    'refact': 'Refact',
}
ORDER = ['OpenHands-Versa', 'GUIRepair-o3', 'Refact']
CONDS = ['with_image', 'without_image']

# How each failure signature reads in a sentence. Kept as a table so the
# report never prints a bare signature name at a reader.
FAIL_TEXT = {
    'disk full':
        'the worker ran out of disk space pulling or building the image',
    # Verified 2026-09-21 by reading all 31 predictions behind this
    # signature - every one is Refact - rather than trusting the log
    # line. Not one of them changes a file the suite can measure. The
    # content-bearing hunks across all 31 are 15 package-lock.json, 11
    # test/browser/karma.config.cjs, 6 test/karma.config.js, 2
    # package.json, 1 yarn.lock and 1 docs page; the rest of each
    # submission is chmod noise, up to 1945 pure mode-change sections in
    # one file. The karma hunks are the agent repointing CHROME_BIN at
    # the system Chrome for its own container, which the official image
    # already does - hence 'Reversed (or previously applied) patch
    # detected' when the re-run finally reached GNU patch.
    #
    # So the old wording, 'would not apply to the repository', blamed the
    # wrong side. There was no fix to place. Saying that plainly matters
    # because the two readings imply opposite follow-ups: a patch the
    # harness cannot apply is a harness bug worth chasing, and an empty
    # submission is the agent's answer. The row is unresolved either way,
    # so no score moves.
    'agent patch did not apply':
        'the agent submitted no source change - only lockfile, manifest '
        'or test-config hunks, none of which the suite measures',
    'missing node module':
        'the test run aborted on a missing node module',
    'source failed to compile':
        'the patched source did not parse',
    'test process OOM':
        'the test process was killed for running out of memory',
    'image build failed: pip/venv':
        'the harness image failed to build',
    'worker died mid-evaluation':
        'the worker was stopped part-way through the test run',
    'ran to completion, no result uploaded':
        'the run finished but no result file was uploaded',
    'image missing from registry':
        'the instance image was not in the registry',
    'image pull / registry error':
        'the image could not be pulled',
    'docker daemon error':
        'the docker daemon failed',
    'timeout':
        'the run exceeded its time limit',
}

PRED_RE = re.compile(
    r'^(.+?)_(before_patch|gold|with_image|without_image)_'
    r'(.+?)_(\d{4}-\d\d-\d\d[T_]\d\d-\d\d-\d\d)Z$')


def predictions():
    """(agent, condition) -> {instance ids the agent submitted a patch for}"""
    out = collections.defaultdict(set)
    for line in open('s3_preds_fresh.txt'):
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        stem = urllib.parse.unquote(parts[3].strip()).split('/')[-1]
        stem = re.sub(r'[.](pred|json)$', '', stem)
        m = PRED_RE.match(stem)
        if not m:
            continue
        inst, cond, agent, _ = m.groups()
        if inst.startswith('django__') or agent not in AGENTS:
            continue
        # GUIRepair ran the two conditions as two separately named
        # agents; a key from one must not be credited to the other.
        if agent.startswith('GUIRepair') and not agent.endswith(cond):
            continue
        out[(AGENTS[agent], cond)].add(inst)
    return out


def verdicts():
    """(agent, condition, instance) -> {test_name: passed}, latest run."""
    big = pd.read_parquet('all_test_results.parquet')
    big, _ = ig.pin_to_one_generation(big)
    latest = big.groupby(['instance_id', 'patch_type',
                          'agent_name'])['timestamp'].transform('max')
    big = big[(big.timestamp == latest)
              & big.patch_type.isin(CONDS)
              & big.agent_name.isin(AGENTS)]
    out = {}
    for (inst, cond, agent), sub in big.groupby(
            ['instance_id', 'patch_type', 'agent_name']):
        if agent.startswith('GUIRepair') and not agent.endswith(cond):
            continue
        out[(AGENTS[agent], cond, inst)] = (
            sub.groupby('test_name')['passed'].all().to_dict())
    return out


def failures():
    """(agent, condition, instance) -> (signature, evidence).

    Read from the worker stdout, sliced at the evaluator's own
    "Evaluating instance ..." marker. One stdout object holds up to seven
    tasks, so an unsliced read attributes a neighbour's traceback to
    whichever task the object happens to be named after.
    """
    try:
        rows = json.load(open('pending_classified.json'))
    except OSError:
        return {}
    return {(r['agent'], r['condition'], r['instance_id']):
            (r['failure'], r.get('evidence', '')) for r in rows}


def empty_runs():
    """(agent, condition, instance) uploaded as a zero-row result file.

    The object exists in S3 and is a valid parquet with the right schema
    and no rows: the run happened and collected no tests. The ingest
    correctly had nothing to add, which is why these never appear in
    all_test_results.parquet and read as if they had never run.
    """
    try:
        d = pd.read_csv('empty_results.csv')
    except OSError:
        return set()
    out = set()
    for r in d.itertuples():
        if r.ptype in ('before_patch', 'gold'):
            continue
        name = AGENTS.get(r.agent)
        if name:
            out.add((name, r.ptype, r.inst))
    return out


def reason(row):
    """A sentence a reader can act on, not a status code repeated."""
    if row['prediction'] == 'none':
        return 'No patch submitted for this instance.'
    if row['run'] == 'no tests':
        return ('The evaluation ran and uploaded a result file, but it '
                'contains no tests at all, so there is nothing to score.')
    if row['run'] == 'failed':
        return ('The evaluation ran and failed: {}.'.format(FAIL_TEXT.get(row['failure'], row['failure'])))
    if row['run'] == 'no result':
        return ('A patch exists but no evaluation result was ever '
                'uploaded, and no worker log explains why.')
    if row['scoring'] == 'no reference':
        return ('Ran {} tests, but this instance has no usable '
                'FAIL_TO_PASS / PASS_TO_PASS reference - its '
                'before_patch/gold pair is missing or quarantined - so '
                'the run cannot be scored.'.format(format(row['tests'], ',')))
    if row['resolved'] == 'undefined':
        return ('The reference lists no FAIL_TO_PASS test, so there is '
                'no bug-fix requirement to check and resolution is '
                'undefined.')
    if row['resolved'] == 'yes':
        return ('Every required FAIL_TO_PASS test passed and no '
                'PASS_TO_PASS test regressed or went missing.')
    drift = ''
    if row.get('drift_clears'):
        drift = (' Every one of those tests also fails in the runs of the '
                 'other agents while gold passes it, so this is the image '
                 'moving under the benchmark rather than the patch.')
    bits = []
    failed = row['f2p_total'] - row['f2p_passed'] - row['f2p_missing']
    if failed:
        bits.append('%d required F2P test(s) failed' % failed)
    if row['f2p_missing']:
        bits.append('%d required F2P test(s) never ran' % row['f2p_missing'])
    if row['p2p_regressed']:
        bits.append('%d P2P test(s) regressed' % row['p2p_regressed'])
    if row['p2p_missing']:
        bits.append('%d P2P test(s) never ran' % row['p2p_missing'])
    return (('Not resolved: ' + '; '.join(bits) + '.' + drift) if bits
            else 'Not resolved.' + drift)


def build():
    universe = pd.read_csv('coverage_by_instance.csv')[
        ['instance_id', 'repo']]
    R = json.load(open('reference_final.json'))
    ref = R['ref']
    quar = {q['instance_id'] for q in R['quarantined']}

    pred = predictions()
    ver = verdicts()
    fail = failures()
    empty = empty_runs()

    # score_agents.py already worked out which failures are shared by
    # every independent agent; reuse its verdict rather than recompute a
    # second, possibly different, definition of drift.
    sc = pd.read_csv('scored_final.csv')
    sc['agent'] = sc.agent.replace(
        {'llm.claude4': 'OpenHands-Versa',
         'GUIRepair-o3-2025-04-16': 'GUIRepair-o3', 'refact': 'Refact'})
    drift_ok = {(r.agent, r.condition, r.instance_id): r.resolved_excl_drift
                for r in sc.itertuples()}

    same = json.load(open('identical_arms.json'))
    identical = collections.defaultdict(set)
    for raw, insts in same.items():
        key = AGENTS.get(raw) or AGENTS.get(raw + '-with_image')
        if key:
            identical[key] |= set(insts)

    rows = []
    for agent in ORDER:
        for r in universe.itertuples():
            inst = r.instance_id
            entry = ref.get(inst)
            for cond in CONDS:
                row = {'agent': agent, 'instance_id': inst, 'repo': r.repo,
                       'condition': cond,
                       'reference': ('quarantined' if inst in quar
                                     else 'ok' if entry else 'missing'),
                       'tier': entry['tier'] if entry else '',
                       'same_patch': inst in identical[agent],
                       'tests': 0, 'raw_pass': 0, 'raw_fail': 0,
                       'f2p_total': 0, 'f2p_passed': 0, 'f2p_missing': 0,
                       'p2p_total': 0, 'p2p_kept': 0, 'p2p_regressed': 0,
                       'p2p_missing': 0, 'resolved': '-',
                       'failure': '', 'evidence': ''}
                v = ver.get((agent, cond, inst))
                if inst in pred[(agent, cond)]:
                    row['prediction'] = 'submitted'
                elif v is not None:
                    # A patch that was recovered locally and evaluated,
                    # but whose .pred was never written back to S3 - the
                    # upload is blocked, not missing. Calling this "no
                    # prediction" would hide a real result.
                    row['prediction'] = 'local only'
                else:
                    row['prediction'] = 'none'

                fk = fail.get((agent, cond, inst))
                if fk:
                    row['failure'], row['evidence'] = fk
                if v is not None:
                    row['run'] = 'completed'
                elif (agent, cond, inst) in empty:
                    row['run'] = 'no tests'
                    if row['prediction'] == 'none':
                        row['prediction'] = 'submitted'
                elif fk:
                    row['run'] = 'failed'
                elif row['prediction'] == 'none':
                    row['run'] = 'not run'
                else:
                    row['run'] = 'no result'

                if v is not None:
                    row['tests'] = len(v)
                    row['raw_pass'] = sum(1 for p in v.values() if p is True)
                    row['raw_fail'] = row['tests'] - row['raw_pass']
                if v is not None and entry:
                    f2p, p2p = entry['f2p'], entry['p2p']
                    row['scoring'] = 'scored'
                    row['f2p_total'] = len(f2p)
                    row['f2p_passed'] = sum(1 for t in f2p
                                            if v.get(t) is True)
                    row['f2p_missing'] = sum(1 for t in f2p if t not in v)
                    row['p2p_total'] = len(p2p)
                    row['p2p_kept'] = sum(1 for t in p2p if v.get(t) is True)
                    row['p2p_missing'] = sum(1 for t in p2p if t not in v)
                    row['p2p_regressed'] = (row['p2p_total']
                                            - row['p2p_kept']
                                            - row['p2p_missing'])
                    if not f2p:
                        row['resolved'] = 'undefined'
                    elif (row['f2p_passed'] == row['f2p_total']
                          and row['p2p_kept'] == row['p2p_total']):
                        row['resolved'] = 'yes'
                    else:
                        row['resolved'] = 'no'
                elif v is not None:
                    row['scoring'] = 'no reference'
                else:
                    row['scoring'] = 'not scored'
                row['drift_clears'] = bool(
                    row['resolved'] == 'no'
                    and drift_ok.get((agent, cond, inst)) is True)
                row['reason'] = reason(row)
                rows.append(row)
    return add_outcome(pd.DataFrame(rows))


# Failures that are the agent's answer, not a gap in our measurement.
# A patch that will not apply, or that leaves the source unparseable,
# has already told us the FAIL_TO_PASS tests do not pass: SWE-bench
# counts exactly these as unresolved. Dropping them instead, which is
# what `resolved` does because it only ever sees runs that produced a
# result file, silently removes an agent's worst instances from its own
# denominator - and not evenly, so it flatters whichever agent failed
# this way most often.
AGENT_FAULT = frozenset({
    'agent patch did not apply',
    'source failed to compile',
})

# 'missing node module' covers two unrelated things, and the specifier
# tells them apart cleanly. Checked against all 14 such rows:
#
#   8 rows  node_modules/...  ours. The openlayers probe passed a
#           `find node_modules ...` path straight to require(), which
#           reads a bare leading word as a package name. Fixed in
#           6598f82; says nothing about the patch.
#
#   6 rows  ./Something       the agent's. Refact ships no new file in
#           any of its 418 predictions - not one - so every relative
#           import it adds for a path that is not already in the tree is
#           necessarily dangling. Verified one row at a time: each of
#           the six is a '+' line in Refact's own patch, from
#           './AppendAnythingProvider' in lib/ to
#           './BpmnSpaceTool.empty-pool.bpmn' in test/spec.
#
# An earlier version of this excluded '*.bpmn' on the theory that a
# fixture is data the test patch owns. That was wrong. The test patch
# for bpmn-js-1928 adds BpmnSpaceTool.participants.bpmn and never
# mentions empty-pool; Refact's patch is what adds the require. Being
# under test/spec does not make a missing file ours, so the extension
# carve-out and the webpack-internal lookahead are both gone.
DANGLING_IMPORT = re.compile(r"Cannot find module '(\.[^']*)'")


def _is_agent_fault(failure, evidence):
    """Did the agent's own patch cause this failure?"""

    if failure in AGENT_FAULT:
        return True
    if failure != 'missing node module':
        return False
    return bool(DANGLING_IMPORT.search(str(evidence)))


def add_outcome(frame):
    """`outcome`: the verdict with agent-side failures counted as losses.

    Three values rather than two, so nothing is hidden. `unmeasured` is
    reserved for the cases where the harness, not the agent, is why
    there is no answer - a worker that ran out of disk, an instance with
    no reference - and those stay out of every rate.
    """

    measured = frame.resolved.map({'yes': 'resolved', 'no': 'unresolved'})
    blame = [_is_agent_fault(f, e)
             for f, e in zip(frame.failure, frame.evidence, strict=False)]
    lost = pd.Series(blame, index=frame.index) & frame.resolved.eq('-')
    frame['outcome'] = measured.where(~lost, 'unresolved').fillna('unmeasured')
    return frame


if __name__ == '__main__':
    d = build()
    d.to_csv('agent_instance_detail.csv', index=False)
    print('wrote agent_instance_detail.csv  (%d rows)' % len(d))
    print('rows cleared by environment drift: %d'
          % int(d.drift_clears.sum()))
    print(d.prediction.value_counts().to_string())
    print()
    print('=== run outcome')
    print(d.run.value_counts().to_string())
    print()
    print('=== classified failures')
    f = d[d.run == 'failed']
    print(f.groupby(['agent', 'failure']).size().to_string())
    print()
    for agent in ORDER:
        a = d[d.agent == agent]
        print(f'=== {agent}')
        print(a.groupby(['condition', 'resolved']).size().to_string())
        print(a.groupby(['condition', 'run']).size().to_string())
        print()
