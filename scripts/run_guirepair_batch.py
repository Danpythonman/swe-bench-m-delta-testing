import concurrent.futures, json, os, subprocess, sys
from pathlib import Path
from urllib.parse import unquote
import boto3

# Operational limits for the GUIRepair batch.
MAX_WORKERS = 10
EXECUTION_TIMEOUT_SECONDS = 30 * 60
LOCK_PATH = Path('guirepair-batch.lock')

def main():
    try:
        lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(lock_fd, str(os.getpid()).encode())
    except FileExistsError:
        print(f'GUIRepair batch already running; refusing duplicate launch ({LOCK_PATH})', flush=True)
        return 2
    try:
        return run_batch()
    finally:
        os.close(lock_fd)
        LOCK_PATH.unlink(missing_ok=True)

def run_batch():
    # Use the active AWS credential chain (environment, shared config, or role).
    # Do not force a profile name: the AWS CLI may be authenticated via SSO,
    # environment variables, or a non-default profile.
    s3=boto3.Session(region_name='us-east-1').client('s3')
    keys=[]
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket='sbmdt-preds'):
        for x in page.get('Contents',[]):
            k=x['Key']; d=unquote(k)
            if 'GUIRepair-o3-2025-04-16-' in d and d.endswith('.pred'):
                if any(f'_{p}_GUIRepair-o3-2025-04-16-{p}_' in d for p in ('with_image','without_image')):
                    keys.append(k)
    out=Path('guirepair-runs'); out.mkdir(exist_ok=True)
    keys = [k for k in sorted(set(keys))
            if not ((out / (unquote(k).split('_with_image_')[0] + '-with_image.json')).exists()
                    and '_with_image_' in unquote(k)
                    and json.loads((out / (unquote(k).split('_with_image_')[0] + '-with_image.json')).read_text()).get('status') in {'Success', 'skipped'})
            and not ((out / (unquote(k).split('_without_image_')[0] + '-without_image.json')).exists()
                     and '_without_image_' in unquote(k)
                     and json.loads((out / (unquote(k).split('_without_image_')[0] + '-without_image.json')).read_text()).get('status') in {'Success', 'skipped'})]
    def run(k):
        d=unquote(k); inst=d.split('_with_image_')[0] if '_with_image_' in d else d.split('_without_image_')[0]
        typ='with_image' if '_with_image_' in d else 'without_image'
        st=out/f'{inst}-{typ}.json'
        return subprocess.run([
            sys.executable,
            'scripts/repair_canary.py',
            '--key', k,
            '--status-file', str(st),
            '--force-claim',
            '--execution-timeout', str(EXECUTION_TIMEOUT_SECONDS),
        ], capture_output=True, text=True).returncode
    # Match Daniel's project default: at most five concurrent EC2 workers.
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for i,r in enumerate(ex.map(run, sorted(set(keys))),1): print(json.dumps({'completed':i,'total':len(set(keys)),'exit_code':r}),flush=True)
if __name__=='__main__': raise SystemExit(main())
