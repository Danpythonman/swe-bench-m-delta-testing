"""Rerun all available patch sides for one evaluator-repair canary."""

import argparse
import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import boto3


def main(instance: str, patches: list[str]) -> None:
    s3 = boto3.Session(profile_name='default', region_name='us-east-1').client(
        's3'
    )
    wanted = set(patches)
    keys = {}
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket='sbmdt-preds'):
        for item in page.get('Contents', []):
            decoded = unquote(item['Key'])
            for patch in wanted - keys.keys():
                if decoded.startswith(f'{instance}_{patch}_'):
                    keys[patch] = item['Key']
                    break
    missing = sorted(wanted - keys.keys())
    if missing:
        raise SystemExit('Missing prediction keys: ' + ', '.join(missing))

    root = Path('matrix-runs')
    root.mkdir(exist_ok=True)

    def run(item):
        patch, key = item
        status = root / f'{instance}-{patch}.json'
        with (root / f'{instance}-{patch}.log').open(
            'w', encoding='utf-8'
        ) as output:
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
        return {'patch': patch, 'exit_code': proc.returncode}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(run, item) for item in sorted(keys.items())]
        for future in concurrent.futures.as_completed(futures):
            print(json.dumps(future.result()), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('instance')
    parser.add_argument(
        '--patch',
        action='append',
        choices=['before_patch', 'gold', 'with_image', 'without_image'],
        default=None,
    )
    args = parser.parse_args()
    main(args.instance, args.patch or ['before_patch', 'gold'])
