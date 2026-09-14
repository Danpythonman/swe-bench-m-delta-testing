"""Run missing reference baselines for evaluated instances."""

import argparse
import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path


def main(
    repository,
    excluded_repositories,
    workers,
    batch_status,
    only_needed,
):
    audit = json.loads(Path('reference_coverage_audit.json').read_text())
    rows = []
    for row in audit['rows']:
        if not row['prediction_key']:
            continue
        if repository is not None and not row['instance'].startswith(
            repository + '__'
        ):
            continue
        if row['instance'].split('__', 1)[0] in excluded_repositories:
            continue
        # ``audit['rows']`` is authoritative here: every row in it is a
        # reference object that the audit could not find in the results
        # bucket.  A local canary status file may be stale (for example,
        # ``Success`` after the upload was interrupted, or ``running`` after
        # the wrapper was terminated), so it must not suppress a needed
        # repair.  Valid references never appear in this list.
        rows.append(row)
    root = Path('reference-runs')
    root.mkdir(exist_ok=True)
    outcomes = []

    def save():
        batch_status.write_text(
            json.dumps(
                {
                    'scheduled': len(rows),
                    'finished': len(outcomes),
                    'outcomes': outcomes,
                },
                indent=2,
            ),
            encoding='utf-8',
        )

    def run(row):
        label = row['instance'] + '-' + row['patch_type']
        status = root / (label + '.json')
        with (root / (label + '.log')).open('w', encoding='utf-8') as output:
            proc = subprocess.run(
                [
                    sys.executable,
                    'scripts/repair_canary.py',
                    '--key',
                    row['prediction_key'],
                    '--status-file',
                    str(status),
                    '--force-claim',
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
        return {
            'instance': row['instance'],
            'patch_type': row['patch_type'],
            'exit_code': proc.returncode,
            'status_file': str(status),
        }

    save()
    print(
        f'Scheduled {len(rows)} reference runs; {workers} workers.', flush=True
    )
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers
    ) as executor:
        futures = [executor.submit(run, row) for row in rows]
        for future in concurrent.futures.as_completed(futures):
            outcome = future.result()
            outcomes.append(outcome)
            save()
            print(json.dumps(outcome), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository')
    parser.add_argument('--exclude-repository', action='append', default=[])
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument(
        '--batch-status',
        type=Path,
        default=Path('reference_batch_status.json'),
    )
    parser.add_argument('--only-needed', action='store_true')
    args = parser.parse_args()
    main(
        args.repository,
        set(args.exclude_repository),
        args.workers,
        args.batch_status,
        args.only_needed,
    )
