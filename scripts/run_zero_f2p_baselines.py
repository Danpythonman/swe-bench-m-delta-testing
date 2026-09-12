"""Rerun only baseline references invalidated by the missing test-patch bug."""

import argparse
import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import boto3


def main(workers: int, retry_failures: bool) -> None:
    wanted = set(
        json.loads(Path('zero_f2p_candidates.json').read_text())['instances']
    )
    status_path = Path('zero_f2p_batch_status.json')
    if retry_failures:
        previous = json.loads(status_path.read_text(encoding='utf-8'))
        wanted = {
            row['instance']
            for row in previous['outcomes']
            if row['exit_code'] != 0
        }
    s3 = boto3.Session(profile_name='default', region_name='us-east-1').client(
        's3'
    )
    keys = {}
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket='sbmdt-preds'):
        for item in page.get('Contents', []):
            key = item['Key']
            decoded = unquote(key)
            for instance in wanted - keys.keys():
                if decoded.startswith(
                    instance + '_before%5Fpatch_'
                ) or decoded.startswith(instance + '_before_patch_'):
                    keys[instance] = key
                    break
    missing = sorted(wanted - keys.keys())
    if missing:
        raise SystemExit(
            'Missing before_patch prediction keys: ' + ', '.join(missing)
        )

    root = Path('reference-runs')
    root.mkdir(exist_ok=True)
    outcomes = []

    def save() -> None:
        status_path.write_text(
            json.dumps(
                {
                    'scheduled': len(keys),
                    'finished': len(outcomes),
                    'outcomes': outcomes,
                },
                indent=2,
            ),
            encoding='utf-8',
        )

    def run(item):
        instance, key = item
        label = instance + '-before_patch'
        status = root / (label + '.json')
        with (root / (label + '.log')).open('w', encoding='utf-8') as output:
            proc = subprocess.run(
                [
                    sys.executable,
                    'scripts/repair_canary.py',
                    '--key',
                    key,
                    '--status-file',
                    str(status),
                    '--force-claim',
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
        return {'instance': instance, 'exit_code': proc.returncode}

    save()
    print(
        f'Scheduled {len(keys)} corrected baseline runs; {workers} workers.',
        flush=True,
    )
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers
    ) as executor:
        futures = [executor.submit(run, item) for item in sorted(keys.items())]
        for future in concurrent.futures.as_completed(futures):
            outcomes.append(future.result())
            save()
            print(json.dumps(outcomes[-1]), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers', type=int, default=10)
    parser.add_argument('--retry-failures', action='store_true')
    args = parser.parse_args()
    main(args.workers, args.retry_failures)
