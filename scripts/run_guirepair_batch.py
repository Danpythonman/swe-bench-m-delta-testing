"""Launch the GUIRepair prediction batch through repair_canary.py.

A lock file guards against two batches running at once, because each
one claims EC2 workers and a duplicate launch doubles the fleet
without doubling the work.
"""
import concurrent.futures
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import boto3

# Operational limits for the GUIRepair batch.
MAX_WORKERS = 10
EXECUTION_TIMEOUT_SECONDS = 30 * 60
LOCK_PATH = Path('guirepair-batch.lock')

BUCKET = 'sbmdt-preds'
AGENT = 'GUIRepair-o3-2025-04-16-'
CONDITIONS = ('with_image', 'without_image')
DONE_STATUSES = {'Success', 'skipped'}


def main():
    try:
        lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(lock_fd, str(os.getpid()).encode())
    except FileExistsError:
        print(
            'GUIRepair batch already running; refusing duplicate launch '
            f'({LOCK_PATH})',
            flush=True,
        )
        return 2
    try:
        return run_batch()
    finally:
        os.close(lock_fd)
        LOCK_PATH.unlink(missing_ok=True)


def list_pred_keys(s3):
    """Every GUIRepair .pred key in the bucket, for both conditions."""
    keys = []
    pages = s3.get_paginator('list_objects_v2').paginate(Bucket=BUCKET)
    for page in pages:
        for entry in page.get('Contents', []):
            key = entry['Key']
            decoded = unquote(key)
            if AGENT not in decoded or not decoded.endswith('.pred'):
                continue
            if any(f'_{c}_{AGENT}{c}_' in decoded for c in CONDITIONS):
                keys.append(key)
    return keys


def already_done(out, decoded, condition):
    """True when this key's status file records a finished run.

    The split is unconditional: when `decoded` does not name this
    condition, `split` returns the whole string and the resulting path
    does not exist, so the check is false. That is the original
    behaviour and the caller relies on it.
    """
    marker = f'_{condition}_'
    status_path = out / (decoded.split(marker)[0] + f'-{condition}.json')
    if not status_path.exists() or marker not in decoded:
        return False
    status = json.loads(status_path.read_text()).get('status')
    return status in DONE_STATUSES


def run_batch():
    # Use the active AWS credential chain (environment, shared config, or
    # role). Do not force a profile name: the AWS CLI may be
    # authenticated via SSO, environment variables, or a non-default
    # profile.
    s3 = boto3.Session(region_name='us-east-1').client('s3')
    out = Path('guirepair-runs')
    out.mkdir(exist_ok=True)

    keys = [
        k for k in sorted(set(list_pred_keys(s3)))
        if not any(already_done(out, unquote(k), c) for c in CONDITIONS)
    ]

    def run(key):
        decoded = unquote(key)
        condition = (
            'with_image' if '_with_image_' in decoded else 'without_image'
        )
        instance = decoded.split(f'_{condition}_')[0]
        status_file = out / f'{instance}-{condition}.json'
        return subprocess.run(
            [
                sys.executable,
                'scripts/repair_canary.py',
                '--key', key,
                '--status-file', str(status_file),
                '--force-claim',
                '--execution-timeout', str(EXECUTION_TIMEOUT_SECONDS),
            ],
            capture_output=True,
            text=True,
        ).returncode

    total = len(keys)
    with concurrent.futures.ThreadPoolExecutor(MAX_WORKERS) as pool:
        for i, code in enumerate(pool.map(run, keys), 1):
            print(
                json.dumps(
                    {'completed': i, 'total': total, 'exit_code': code}
                ),
                flush=True,
            )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
