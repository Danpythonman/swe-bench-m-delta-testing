"""Rerun only missing predictions with a verified newline syntax defect."""

import argparse
import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path

import boto3
from audit_prediction_coverage import identity

ADDITIONAL_RECOVERABLE = {
    ('bpmn-io__bpmn-js-1382', 'without_image'),
    ('carbon-design-system__carbon-11664', 'without_image'),
    ('carbon-design-system__carbon-12398', 'without_image'),
    ('carbon-design-system__carbon-13317', 'without_image'),
    ('carbon-design-system__carbon-13364', 'with_image'),
    ('carbon-design-system__carbon-8720', 'with_image'),
    ('carbon-design-system__carbon-8912', 'without_image'),
    ('carbon-design-system__carbon-9136', 'without_image'),
    ('openlayers__openlayers-14945', 'without_image'),
}


def main(
    excluded_repositories: set[str],
    include_recoverable: bool,
    retry_failed: bool,
    additional_only: bool,
    workers: int,
    batch_status: Path,
):
    s3 = boto3.Session(profile_name='default').client('s3')
    present = {
        identity(obj['Key'])
        for page in s3.get_paginator('list_objects_v2').paginate(
            Bucket='sbmdt-test-results'
        )
        for obj in page.get('Contents', [])
    }
    audit = json.loads(Path('coverage_audit.json').read_text())
    attempted = set()
    for path in Path('repair-runs').glob('*.json'):
        prior = json.loads(path.read_text(encoding='utf-8'))
        ident = identity(prior.get('prediction_key', ''), True)
        status = prior.get('status')
        if ident and (not retry_failed or status in {'Success', 'running'}):
            attempted.add(ident[:2])
    candidates = [
        r
        for r in audit['rows']
        if not r['result_present']
        and (
            (
                not additional_only
                and not r['syntax_valid']
                and r['newline_repair_valid']
            )
            or (
                include_recoverable
                and (r['instance'], r['variant']) in ADDITIONAL_RECOVERABLE
            )
        )
        and identity(r['key'], True) not in present
        and r['instance'].split('__', 1)[0] not in excluded_repositories
        and (r['instance'], r['variant']) not in attempted
    ]
    root = Path('repair-runs')
    root.mkdir(exist_ok=True)
    outcomes = []

    def save():
        batch_status.write_text(
            json.dumps(
                {
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
        status = root / (label + '.json')
        with (root / (label + '.log')).open('w', encoding='utf-8') as output:
            proc = subprocess.run(
                [
                    sys.executable,
                    'scripts/repair_canary.py',
                    '--key',
                    row['key'],
                    '--status-file',
                    str(status),
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
        return dict(
            instance=row['instance'],
            variant=row['variant'],
            exit_code=proc.returncode,
            status_file=str(status),
        )

    save()
    print(
        f'Scheduled {len(candidates)} missing predictions; {workers} workers.',
        flush=True,
    )
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers
    ) as executor:
        futures = [executor.submit(run, row) for row in candidates]
        for future in concurrent.futures.as_completed(futures):
            outcome = future.result()
            outcomes.append(outcome)
            save()
            print(json.dumps(outcome), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exclude-repo', action='append', default=[])
    parser.add_argument('--include-recoverable', action='store_true')
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--additional-only', action='store_true')
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument(
        '--batch-status', type=Path, default=Path('repair_batch_status.json')
    )
    args = parser.parse_args()
    main(
        set(args.exclude_repo),
        args.include_recoverable,
        args.retry_failed,
        args.additional_only,
        args.workers,
        args.batch_status,
    )
