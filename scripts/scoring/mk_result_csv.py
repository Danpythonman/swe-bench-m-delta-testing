"""result.csv - one row per instance, agent and condition.

Daniel asked for the swe-bench outcome "in result.csv". The unit that
answers every question asked of this campaign is (instance, agent,
condition), so that is the row. Columns are ordered so the first six
answer "did it resolve, and if not, why not" without scrolling.

`resolved` is the harness verdict: every F2P test passes and no P2P test
was lost. `resolved_excl_drift` repeats it after discarding tests that
every independent agent fails while gold passes - those are the :latest
image having moved since the references were taken, not the patch.
"""
import pandas as pd

d = pd.read_csv('agent_instance_detail.csv')

COLS = [
    'instance_id', 'repo', 'agent', 'condition',
    # `outcome` before `resolved` because it is the one to report. They
    # differ only where the run produced no result file: `resolved` is
    # blank there whatever the cause, while `outcome` separates a
    # harness gap we should not score ("unmeasured") from the agent
    # having failed outright - a patch that would not apply, or source
    # that would not parse - which is an "unresolved" instance and
    # belongs in the denominator.
    'outcome',
    'resolved', 'run', 'failure',
    'reference', 'tier',
    'f2p_total', 'f2p_passed', 'f2p_missing',
    'p2p_total', 'p2p_kept', 'p2p_regressed', 'p2p_missing',
    'tests', 'raw_pass', 'raw_fail',
    'prediction', 'scoring', 'drift_clears', 'same_patch',
    'evidence', 'reason',
]
out = d[[c for c in COLS if c in d.columns]].copy()

# same_patch is per instance, and on its own it does not stop a reader
# doing the one thing this data cannot support: reading a with-image
# against a without-image column as an experiment. Whether that
# comparison exists at all is a fact about the agent, not the instance,
# so it belongs on every row.
#
# Verified 2026-09-21 by hashing model_patch across the two *agent
# names* - not the two condition labels, which for GUIRepair compares
# its duplicated upload with itself and always says "identical",
# answering nothing. Only Refact has two genuine arms.
ARM_STATUS = {
    'GUIRepair-o3': 'duplicate: 186/186 identical, no image condition',
    'OpenHands-Versa': 'duplicate: 152/161 copied, only 10 genuine',
    'Refact': 'genuine: 0/159 identical, two experiment directories',
}
out['arm_status'] = out.agent.map(ARM_STATUS).fillna('unverified')

out = out.sort_values(['instance_id', 'agent', 'condition'])
out.to_csv('result.csv', index=False)
print('result.csv: %d rows x %d cols' % out.shape)
for agent, status in sorted(ARM_STATUS.items()):
    print('  %-16s %s' % (agent, status))
print()
print(out.groupby(['agent', 'condition'])['resolved']
      .value_counts().unstack(fill_value=0))
