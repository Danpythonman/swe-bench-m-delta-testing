"""Audit latest before/gold reference outcomes without changing scores."""

import json
from pathlib import Path

from build_benchmark_pdf import load_parquets


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    wanted = set(
        json.loads((root / 'zero_f2p_candidates.json').read_text())[
            'instances'
        ]
    )
    frame = load_parquets(root / 'live-results')
    refs = frame[
        frame['instance_id'].isin(wanted)
        & frame['patch_type'].isin(['before_patch', 'gold'])
    ].copy()
    latest = refs.groupby(
        ['instance_id', 'patch_type', 'agent_name'], observed=True
    )['timestamp'].transform('max')
    refs = refs[refs['timestamp'] == latest]
    rows = []
    for instance in sorted(wanted):
        sides = {}
        for patch in ('before_patch', 'gold'):
            part = refs[
                (refs['instance_id'] == instance)
                & (refs['patch_type'] == patch)
            ]
            sides[patch] = dict(
                zip(
                    part['test_name'],
                    part['passed'].astype(bool),
                    strict=True,
                )
            )
        pre, gold = sides['before_patch'], sides['gold']
        common = pre.keys() & gold.keys()
        rows.append(
            {
                'instance': instance,
                'before_tests': len(pre),
                'before_failed': sum(not value for value in pre.values()),
                'gold_tests': len(gold),
                'gold_failed': sum(not value for value in gold.values()),
                'common_tests': len(common),
                'fail_to_pass': sum(
                    not pre[name] and gold[name] for name in common
                ),
                'before_only': len(pre.keys() - gold.keys()),
                'gold_only': len(gold.keys() - pre.keys()),
            }
        )
    (root / 'reference_delta_audit.json').write_text(
        json.dumps({'rows': rows}, indent=2), encoding='utf-8'
    )
    for row in rows:
        print(json.dumps(row), flush=True)


if __name__ == '__main__':
    main()
