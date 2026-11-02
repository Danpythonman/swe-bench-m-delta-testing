"""Write one workbook per agent, plus a combined one, from the detail CSV.

The CSV is the source of truth for Part 6: `scored` is a row that reached
a verdict (`resolved` is True/False rather than '-'), `resolved` is a row
whose verdict was True. Counting anything else here would drift from the
report.
"""
import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ORDER = ['OpenHands-Versa', 'GUIRepair-o3', 'Refact']
CONDS = ['with_image', 'without_image']
COLS = [
    'instance_id', 'repo', 'condition', 'prediction', 'run', 'scoring',
    'resolved', 'outcome', 'reference', 'tier', 'same_patch',
    'f2p_total', 'f2p_passed', 'f2p_missing',
    'p2p_total', 'p2p_kept', 'p2p_regressed', 'p2p_missing',
    'tests', 'raw_pass', 'raw_fail',
    'failure', 'evidence', 'drift_clears', 'reason',
]

d = pd.read_csv('agent_instance_detail.csv')
have = [c for c in COLS if c in d.columns]
missing = [c for c in COLS if c not in d.columns]
if missing:
    print('note: columns absent from the CSV, skipped:', missing)

HEAD = PatternFill('solid', fgColor='1F3A5F')
BOLD = Font(bold=True, color='FFFFFF')


def sheet(ws, frame):
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    for cell in ws[1]:
        cell.fill = HEAD
        cell.font = BOLD
        cell.alignment = Alignment(vertical='center', wrap_text=True)
    for i, col in enumerate(frame.columns, start=1):
        width = max(len(str(col)), *(len(str(v)) for v in frame[col].head(400)))
        ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 9), 60)
    ws.row_dimensions[1].height = 28


def summary(frame):
    rows = []
    for agent in ORDER:
        for cond in CONDS:
            f = frame[(frame.agent == agent) & (frame.condition == cond)]
            predicted = int((f.prediction != 'none').sum())
            # `resolved` is yes/no/'-': '-' means we never reached a
            # verdict. Counting it as a failure would silently turn
            # every unrun instance into a loss for the agent.
            scored = int(f.resolved.isin(['yes', 'no']).sum())
            resolved = int(f.resolved.eq('yes').sum())
            rows.append({
                'agent': agent,
                'condition': cond.replace('_', ' '),
                'instances': len(f),
                'predicted': predicted,
                'scored': scored,
                'resolved': resolved,
                'rate': round(100 * resolved / scored, 1) if scored else 0.0,
            })
    return pd.DataFrame(rows)


s = summary(d)
print(s.to_string(index=False))

with pd.ExcelWriter('agent_instances_all.xlsx', engine='openpyxl') as xl:
    s.to_excel(xl, sheet_name='Summary', index=False)
    sheet(xl.sheets['Summary'], s)
    for agent in ORDER:
        f = d[d.agent == agent][have].sort_values(['condition', 'instance_id'])
        name = agent[:31]
        f.to_excel(xl, sheet_name=name, index=False)
        sheet(xl.sheets[name], f)
print('wrote agent_instances_all.xlsx')

for agent in ORDER:
    f = d[d.agent == agent][have].sort_values(['condition', 'instance_id'])
    path = 'instances_{}.xlsx'.format(agent.replace('-', '_').replace('.', ''))
    with pd.ExcelWriter(path, engine='openpyxl') as xl:
        f.to_excel(xl, sheet_name='instances', index=False)
        sheet(xl.sheets['instances'], f)
    print('wrote %-34s %4d rows' % (path, len(f)))
