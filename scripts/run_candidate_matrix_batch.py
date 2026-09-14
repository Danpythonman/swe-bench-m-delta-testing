"""Run selected patch sides for candidate instances in one repository."""

import argparse
import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import boto3


def main(repository: str, patches: set[str], workers: int) -> None:
    candidates = json.loads(Path('zero_f2p_candidates.json').read_text())[
        'instances'
    ]
    wanted_instances = {
        item for item in candidates if item.startswith(repository + '__')
    }
    wanted = {
        (instance, patch) for instance in wanted_instances for patch in patches
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
            for identity in wanted:
                if decoded.startswith(f'{identity[0]}_{identity[1]}_'):
                    if identity not in keys or decoded > unquote(
                        keys[identity]
                    ):
                        keys[identity] = key
                    break
    missing = sorted(wanted - keys.keys())
    if missing:
        raise SystemExit('Missing prediction keys: ' + repr(missing))

    root = Path('matrix-batch-runs')
    root.mkdir(exist_ok=True)
    batch = Path(f'{repository}_matrix_batch_status.json')
    outcomes = []

    def save():
        batch.write_text(
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
        (instance, patch), key = item
        label = f'{instance}-{patch}'
        status = root / f'{label}.json'
        with (root / f'{label}.log').open('w', encoding='utf-8') as output:
            proc = subprocess.run(
                [
                    sys.executable,
                    'scripts/repair_canary.py',
                    '--key',
                    key,
                    '--status-file',
                    str(status),
                    '--force-claim',
                    '--execution-timeout',
                    '5400',
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
        return {
            'instance': instance,
            'patch': patch,
            'exit_code': proc.returncode,
        }

    save()
    print(f'Scheduled {len(keys)} runs with {workers} workers.', flush=True)
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
    parser.add_argument('repository')
    parser.add_argument('--patch', action='append', required=True)
    parser.add_argument('--workers', type=int, default=5)
    args = parser.parse_args()
    main(args.repository, set(args.patch), args.workers)
