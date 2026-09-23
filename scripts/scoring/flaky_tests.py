"""Find tests that give different verdicts on byte-identical code.

GUIRepair's two conditions carry the same patch for every instance, and
OpenHands' do for 152 (identical_arms.json, written by arm_validity.py).
Each such pair is one piece of code run twice. Where both runs come from
the same campaign and harness epoch, and both ran nearly the whole suite,
a test that passes in one and fails in the other is flaky - observed
directly, not inferred.

A test that flips on a single instance could be one agent's racy patch
rather than the test itself, so a test must flip on at least
MIN_INSTANCES distinct instances to be listed. Measured on 23 September:
49 tests (46 openlayers, 2 carbon, 1 alibaba), led by the ol/View
#animate() and #cancelAnimations() timing tests at 18-26 instances each.

score_final drops the listed tests from every FAIL_TO_PASS and
PASS_TO_PASS list, for every agent alike. An instance whose FAIL_TO_PASS
consisted only of them is left with nothing to demonstrate the fix and
stops being gradeable - 23 openlayers instances, which were scoring
whichever way a timing test happened to fall.

Writes flaky_tests.csv (repo, test, instances, where).
"""
import collections
import json
import os
import subprocess

import image_generation as ig
import pandas as pd

MIN_INSTANCES = 2
COMPLETE = 0.90
REPO = os.environ.get('SBMDT_REPO', 'swe-bench-m-delta-testing')
BRANCH = os.environ.get('SBMDT_BRANCH', 'HEAD')
EVALUATOR = {
    'openlayers': 'openlayers', 'bpmn-io': 'bpmn',
    'carbon-design-system': 'carbon', 'alibaba-fusion': 'alibaba',
    'GoogleChrome': 'lighthouse', 'eslint': 'eslint', 'grommet': 'grommet',
    'highlightjs': 'highlightjs', 'PrismJS': 'prismjs',
    'prettier': 'prettier', 'quarto-dev': 'quarto',
    'scratchfoundation': 'scratchgui',
}


def fix_times(repo_prefix):
    """Commit times of changes to this repo's evaluator or the shared base."""
    paths = ['src/sbmdt/evaluator/%s' % EVALUATOR[repo_prefix],
             'src/sbmdt/evaluator/base.py']
    out = subprocess.run(
        ['git', '-C', REPO, 'log', '--no-merges', '--format=%aI', BRANCH,
         '--', *paths], capture_output=True, text=True).stdout.split()
    return sorted(pd.Timestamp(t).tz_convert('UTC') for t in out)


def main():
    ident = json.load(open('identical_arms.json'))
    same = {
        'GUIRepair': set(ident.get('GUIRepair-o3-2025-04-16-with_image', [])),
        'llm.claude4': set(ident.get('llm.claude4', [])),
    }
    insts = sorted(same['GUIRepair'] | same['llm.claude4'])
    big = pd.read_parquet(
        'all_test_results.parquet',
        filters=[('instance_id', 'in', insts),
                 ('patch_type', 'in', ['with_image', 'without_image'])])
    big = ig.drop_excluded(big, verbose=False)
    big = big[big.agent_name.isin([
        'llm.claude4', 'GUIRepair-o3-2025-04-16-with_image',
        'GUIRepair-o3-2025-04-16-without_image'])]
    big = big.assign(gen=ig.generation_of(big.timestamp).to_numpy())
    fixes = {r: fix_times(r) for r in EVALUATOR}

    runs = collections.defaultdict(list)
    for (inst, pt, agent, ts), grp in big.groupby(
            ['instance_id', 'patch_type', 'agent_name', 'timestamp']):
        if agent.startswith('GUIRepair') and not agent.endswith(pt):
            continue
        arm = 'GUIRepair' if agent.startswith('GUIRepair') else agent
        if inst not in same[arm]:
            continue
        epoch = sum(1 for f in fixes[inst.split('__')[0]] if f <= ts)
        runs[(inst, arm)].append(
            (pt, grp.gen.iloc[0], epoch,
             grp.groupby('test_name').passed.all().to_dict()))

    flips = collections.defaultdict(set)
    pairs = 0
    for (inst, _), rs in runs.items():
        for a in (r for r in rs if r[0] == 'with_image'):
            for b in (r for r in rs if r[0] == 'without_image'):
                if a[1:3] != b[1:3]:
                    continue
                va, vb = a[3], b[3]
                if min(len(va), len(vb)) < COMPLETE * max(len(va), len(vb)):
                    continue
                pairs += 1
                for t in set(va) & set(vb):
                    if va[t] != vb[t]:
                        flips[(inst.split('__')[0], t)].add(inst)

    rows = [dict(repo=r, test=t, instances=len(s),
                 where=' '.join(sorted(s)))
            for (r, t), s in flips.items() if len(s) >= MIN_INSTANCES]
    out = pd.DataFrame(rows, columns=['repo', 'test', 'instances', 'where'])
    out.sort_values(['repo', 'instances'], ascending=[True, False]).to_csv(
        'flaky_tests.csv', index=False)
    print('identical-code run pairs compared: %d' % pairs)
    print('tests that flip on >= %d instances: %d  (%s)' % (
        MIN_INSTANCES, len(out),
        ', '.join('%s %d' % kv for kv in
                  out.groupby('repo').size().items())))
    print('wrote flaky_tests.csv')


if __name__ == '__main__':
    main()
