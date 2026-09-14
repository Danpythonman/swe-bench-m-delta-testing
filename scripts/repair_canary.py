"""Run one newline-repair canary on an automatically terminated EC2 worker."""

import argparse
import io
import json
import os
import re
import shlex
import tarfile
import time
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ConnectionError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

session = boto3.Session(profile_name='default', region_name='us-east-1')
config = Config(
    connect_timeout=5,
    read_timeout=15,
    retries={'mode': 'standard', 'max_attempts': 3},
)
ec2 = session.client('ec2', config=config)
ssm = session.client('ssm', config=config)
key = (
    'alibaba-fusion%5F%5Fnext-1063_without%5Fimage_'
    'llm.claude4_2026-06-17_20-40-31Z.pred'
)
state = {}
status_path = Path('canary_status.json')
force_claim = False
TRANSIENT_AWS_ERRORS = (
    ConnectTimeoutError,
    ConnectionError,
    EndpointConnectionError,
    ReadTimeoutError,
)


def aws_retry(call, attempts: int = 5):
    """Retry idempotent AWS operations across transient endpoint failures."""
    for attempt in range(attempts):
        try:
            return call()
        except TRANSIENT_AWS_ERRORS:
            if attempt == attempts - 1:
                raise
            time.sleep(min(2 ** (attempt + 1), 20))
    raise AssertionError('unreachable')


def minimal_dockerfile(content: bytes) -> bytes:
    """Keep the SWE-bench image and omit unused nested-runner tooling.

    The published ``swebench/sweb.eval`` image already contains the checked
    out repository and its test dependencies. The generated Dockerfiles add
    Docker CLI, Arrow, and a second SWE-bench checkout, none of which is used
    by these evaluators. Omitting those layers saves most of the worker's
    bounded lifetime for the actual test suite.
    """
    first = next(
        (
            line.strip()
            for line in content.decode('utf-8').splitlines()
            if line.strip().startswith('FROM ')
        ),
        None,
    )
    if first is None:
        raise ValueError('Dockerfile has no FROM instruction')
    return f'{first}\n\nWORKDIR /testbed\n'.encode()


def prediction_identity(prediction_key: str):
    decoded = unquote(prediction_key)
    match = __import__('re').match(
        r'^(.*?)_(before_patch|gold|with_image|without_image)_(.*?)\.pred$',
        decoded,
    )
    return match.groups() if match else None


def save():
    status_path.write_text(json.dumps(state, indent=2), encoding='utf-8')
    print(json.dumps(state), flush=True)


def main():
    instance = None
    run_id = uuid4().hex
    asset_key = 'repair-assets/newline-' + run_id + '.tar.gz'
    s3 = session.client('s3', config=config)
    try:
        pred_identity = prediction_identity(key)
        if pred_identity is None:
            raise ValueError('Unrecognized prediction key')
        claim_root = Path('repair-claims')
        claim_root.mkdir(exist_ok=True)
        claim_name = re.sub(
            r'[^A-Za-z0-9_.-]+', '_', f'{pred_identity[0]}-{pred_identity[1]}'
        )
        claim_path = claim_root / claim_name
        if force_claim and claim_path.exists():
            claim_path.unlink()
        try:
            claim = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            print(f'Skipping already claimed run: {claim_name}', flush=True)
            return
        else:
            os.close(claim)
        state['prediction_key'] = key
        root = Path(__file__).resolve().parents[1]
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w:gz') as tar:
            files = list((root / 'src').rglob('*.py'))
            files += [
                root / 'scripts/run_instance.py',
                root / 'aws/run_ec2.sh',
                root / 'pyproject.toml',
                root / 'uv.lock',
            ]
            instance_files = root / 'dockerfiles' / pred_identity[0]
            if not instance_files.is_dir():
                raise FileNotFoundError(
                    f'No local assets for {pred_identity[0]}'
                )
            files += [
                path for path in instance_files.rglob('*') if path.is_file()
            ]
            for source in files:
                # Git for Windows may check out shell scripts as CRLF.
                content = source.read_bytes()
                if source.name == 'Dockerfile':
                    content = minimal_dockerfile(content)
                if source.suffix in ('.py', '.sh', '.toml'):
                    content = content.replace(b'\r\n', b'\n')
                info = tarfile.TarInfo(source.relative_to(root).as_posix())
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        archive_bytes = archive.getvalue()
        aws_retry(
            lambda: s3.put_object(
                Bucket='sbmdt-stdout', Key=asset_key, Body=archive_bytes
            )
        )
        state['asset_key'] = asset_key
        response = aws_retry(
            lambda: ec2.run_instances(
                ClientToken=run_id,
                ImageId='ami-070ff54484e26f9bb',
                InstanceType='t3a.large',
                MinCount=1,
                MaxCount=1,
                SubnetId='subnet-0d1aeaaad9a22c741',
                SecurityGroupIds=['sg-09f9a76d742f8549d'],
                IamInstanceProfile={
                    'Arn': 'arn:aws:iam::607869540801:instance-profile/'
                    'sbmdt-instance-profile'
                },
                InstanceInitiatedShutdownBehavior='terminate',
                UserData='#!/bin/bash\nshutdown -h +75\n',
                BlockDeviceMappings=[
                    {
                        'DeviceName': '/dev/xvda',
                        'Ebs': {
                            'VolumeSize': 32,
                            'VolumeType': 'gp3',
                            'DeleteOnTermination': True,
                        },
                    }
                ],
                TagSpecifications=[
                    {
                        'ResourceType': 'instance',
                        'Tags': [
                            {
                                'Key': 'Name',
                                'Value': 'sbmdt-newline-repair-canary',
                            }
                        ],
                    }
                ],
            )
        )
        instance = response['Instances'][0]['InstanceId']
        state.update(instance=instance, status='waiting_for_ssm')
        save()
        deadline = time.monotonic() + 360
        while time.monotonic() < deadline:
            try:
                registered = ssm.describe_instance_information(
                    Filters=[{'Key': 'InstanceIds', 'Values': [instance]}]
                )
            except TRANSIENT_AWS_ERRORS as error:
                state['poll_error'] = str(error)
                save()
                time.sleep(10)
                continue
            if registered.get('InstanceInformationList'):
                break
            time.sleep(10)
        else:
            raise TimeoutError('Worker did not register with SSM')
        command = 'set -eu\ncd /opt/sbmdt\n'
        command += (
            shlex.join(
                [
                    'aws',
                    's3',
                    'cp',
                    's3://sbmdt-stdout/' + asset_key,
                    '/tmp/repair-harness.tar.gz',
                ]
            )
            + '\n'
        )
        command += 'tar -xzf /tmp/repair-harness.tar.gz -C /opt/sbmdt\n'
        command += shlex.join(
            [
                'bash',
                'aws/run_ec2.sh',
                '--instance-id',
                pred_identity[0],
                '--patch-type',
                pred_identity[1],
                '--pred-bucket',
                'sbmdt-preds',
                '--pred-key',
                key,
                '--results-bucket',
                'sbmdt-test-results',
                '--stdout-bucket',
                'sbmdt-stdout',
                '--stdout-key',
                'repair-canary/' + run_id + '.log',
            ]
        )
        if pred_identity[1] in (
            'before_patch',
            'with_image',
            'without_image',
        ):
            command += ' --apply-test-patch'
        sent = ssm.send_command(
            InstanceIds=[instance],
            DocumentName='AWS-RunShellScript',
            Parameters={'commands': [command], 'executionTimeout': ['4200']},
        )
        command_id = sent['Command']['CommandId']
        state.update(command_id=command_id, status='running')
        save()
        deadline = time.monotonic() + 4260
        while time.monotonic() < deadline:
            time.sleep(15)
            try:
                result = ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=instance
                )
            except TRANSIENT_AWS_ERRORS as error:
                state['poll_error'] = str(error)
                save()
                continue
            if result['Status'] in ('Pending', 'InProgress', 'Delayed'):
                continue
            state.update(
                status=result['Status'],
                stdout=result.get('StandardOutputContent'),
                stderr=result.get('StandardErrorContent'),
            )
            save()
            if result['Status'] != 'Success':
                raise RuntimeError(
                    'Canary command failed; inspect canary_status.json'
                )
            break
        else:
            raise TimeoutError('Canary evaluation timed out')
    except Exception as error:
        state.update(status='error', error=str(error))
        save()
        raise
    finally:
        if instance:
            aws_retry(lambda: ec2.terminate_instances(InstanceIds=[instance]))
            state['termination_requested'] = True
            save()
        aws_retry(
            lambda: s3.delete_object(Bucket='sbmdt-stdout', Key=asset_key)
        )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key', default=key)
    parser.add_argument('--status-file', type=Path, default=status_path)
    parser.add_argument('--force-claim', action='store_true')
    args = parser.parse_args()
    key = args.key
    status_path = args.status_file
    force_claim = args.force_claim
    main()
