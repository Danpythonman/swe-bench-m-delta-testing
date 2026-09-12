"""Evaluate every missing row in the matched local prediction pairs."""

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
from pathlib import Path


def main(workers: int):
    root = Path(__file__).resolve().parents[1]
    with_dir = root.parent / 'with-images-recovered'
    without_dir = root.parent / 'without-images'
    with_ids = {path.stem for path in with_dir.glob('*.pred')}
    without_ids = {path.stem for path in without_dir.glob('*.pred')}
    paired = with_ids & without_ids

    audit = json.loads((root / 'coverage_audit.json').read_text())
    run_root = root / 'paired-runs'
    run_root.mkdir(exist_ok=True)
    candidates = []
    for row in audit['rows']:
        if (
            row['instance'] not in paired
            or row['variant'] not in {'with_image', 'without_image'}
            or row['result_present']
        ):
            continue
        prior_path = run_root / (
            row['instance'] + '-' + row['variant'] + '.json'
        )
        if prior_path.exists():
            try:
                prior = json.loads(prior_path.read_text(encoding='utf-8'))
            except Exception:
                prior = {}
            # The coverage audit is authoritative: this row is missing from
            # the results bucket. A local ``error``/``running`` status can be
            # stale, so only a valid completed result should suppress retry.
            if prior.get('status') == 'Success':
                continue
        candidates.append(row)

    claim_root = root / 'repair-claims'
    claim_root.mkdir(exist_ok=True)
    for row in candidates:
        claim_name = re.sub(
            r'[^A-Za-z0-9_.-]+',
            '_',
            f'{row["instance"]}-{row["variant"]}',
        )
        (claim_root / claim_name).unlink(missing_ok=True)
    outcomes = []
    status_path = root / 'paired_batch_status.json'

    def save():
        status_path.write_text(
            json.dumps(
                {
                    'paired_instances': len(paired),
                    'scheduled': len(candidates),
                    'finished': len(outcomes),
                    'outcomes': outcomes,
                },
                indent=2,
            ),
            encoding='utf-8',
        )

    def run(row):
        label = row['instance'] + '-' + row['variant']
        result_status = run_root / (label + '.json')
        with (run_root / (label + '.log')).open(
            'w', encoding='utf-8'
        ) as output:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(root / 'scripts' / 'repair_canary.py'),
                    '--key',
                    row['key'],
                    '--status-file',
                    str(result_status),
                    '--force-claim',
                ],
                cwd=root,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
        return {
            'instance': row['instance'],
            'variant': row['variant'],
            'exit_code': proc.returncode,
            'status_file': str(result_status),
        }

    save()
    print(
        f'{len(paired)} genuine pairs; scheduled {len(candidates)} missing '
        f'variant runs with {workers} workers.',
        flush=True,
    )
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers
    ) as executor:
        futures = [executor.submit(run, row) for row in candidates]
        for future in concurrent.futures.as_completed(futures):
            outcomes.append(future.result())
            save()
            print(json.dumps(outcomes[-1]), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers', type=int, default=8)
    args = parser.parse_args()
    main(args.workers)
