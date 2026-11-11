"""The hosted SWE-DT evaluation API (one Lambda behind API Gateway).

Client routes (API key required, see aws/api.yaml):

    POST /v1/evaluations          {"instance_id", "patch", "agent_name"?}
    GET  /v1/evaluations/{id}     status, and the verdict once done
    GET  /v1/instances            instance ids that can be evaluated

Worker routes (no API key; the per-job token in X-Job-Token instead):

    GET  /v1/jobs/{id}/patch      the job's .pred
    POST /v1/jobs/{id}/result     {"status": "DONE"|"FAILED", ...}

A submission starts one EC2 worker from the launch template. The worker has
no IAM role: it fetches its patch and reports its verdict through the two
worker routes, and terminates itself. Limits: at most MAX_WORKERS workers
at once, DAILY_LIMIT evaluations per key per UTC day, patches up to
MAX_PATCH_BYTES, and workers older than STALE_MINUTES are terminated (and
their jobs marked TIMEOUT) whenever the API is called.

Standard library and boto3 only, so the code needs no packaging.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

HERE = Path(__file__).parent
TABLE = boto3.resource('dynamodb').Table(os.environ['TABLE'])
S3 = boto3.client('s3')
EC2 = boto3.client('ec2')
SSM = boto3.client('ssm')

BUCKET = os.environ['BUCKET']
LAUNCH_TEMPLATE_ID = os.environ['LAUNCH_TEMPLATE_ID']
HARNESS_COMMIT = os.environ['HARNESS_COMMIT']
ENABLED_PARAMETER = os.environ['ENABLED_PARAMETER']
MAX_WORKERS = int(os.environ.get('MAX_WORKERS', '5'))
DAILY_LIMIT = int(os.environ.get('DAILY_LIMIT', '20'))
MAX_PATCH_BYTES = int(os.environ.get('MAX_PATCH_BYTES', str(1 << 20)))
RUN_MINUTES = int(os.environ.get('RUN_MINUTES', '60'))
STALE_MINUTES = int(os.environ.get('STALE_MINUTES', '75'))

WORKER_TAG = 'sbmdt-api'
JOB_TAG = 'sbmdt-api-job'
INSTANCES = frozenset(json.loads((HERE / 'instances.json').read_text()))
USER_DATA = (HERE / 'worker-userdata.sh').read_text()
AGENT_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
JOB_ID = re.compile(r'^[0-9a-f]{32}$')
FINISHED = {'DONE', 'FAILED', 'TIMEOUT'}

_enabled_cache: tuple[float, bool] = (0.0, True)


class Reply(Exception):
    """An HTTP error reply, raised from anywhere in a route."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    route = (event.get('httpMethod'), event.get('resource'))
    routes = {
        ('POST', '/v1/evaluations'): submit,
        ('GET', '/v1/evaluations/{id}'): status,
        ('GET', '/v1/instances'): instances,
        ('GET', '/v1/jobs/{id}/patch'): worker_patch,
        ('POST', '/v1/jobs/{id}/result'): worker_result,
    }
    try:
        if route not in routes:
            raise Reply(404, 'no such route')
        return reply(200, routes[route](event))
    except Reply as error:
        return reply(error.status, {'error': error.message})


def reply(code: int, body: Any) -> dict[str, Any]:
    status_code = body.pop('_status', code) if isinstance(body, dict) else code
    return {
        'statusCode': status_code,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps(body),
    }


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso(moment: dt.datetime) -> str:
    return moment.isoformat(timespec='seconds')


def api_key_id(event: dict[str, Any]) -> str:
    key = event.get('requestContext', {}).get('identity', {}).get('apiKeyId')
    if not key:
        raise Reply(403, 'an API key is required')
    return key


def body_json(event: dict[str, Any], limit: int) -> dict[str, Any]:
    raw = event.get('body') or ''
    if event.get('isBase64Encoded'):
        raw = base64.b64decode(raw).decode('utf-8', 'replace')
    if len(raw.encode()) > limit:
        raise Reply(413, f'request body over {limit} bytes')
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise Reply(400, f'body is not JSON: {error}') from None
    if not isinstance(data, dict):
        raise Reply(400, 'body must be a JSON object')
    return data


# ---------------------------------------------------------------- clients


def submit(event: dict[str, Any]) -> dict[str, Any]:
    key = api_key_id(event)
    if not enabled():
        raise Reply(503, 'evaluations are paused; try again later')
    data = body_json(event, MAX_PATCH_BYTES + 4096)
    instance = data.get('instance_id')
    patch = data.get('patch')
    agent = data.get('agent_name') or 'api'
    if instance not in INSTANCES:
        raise Reply(
            400, 'unknown instance_id; GET /v1/instances lists valid ones'
        )
    if not isinstance(patch, str) or not patch.strip():
        raise Reply(400, 'patch must be a non-empty unified diff string')
    if len(patch.encode()) > MAX_PATCH_BYTES:
        raise Reply(413, f'patch over {MAX_PATCH_BYTES} bytes')
    if not isinstance(agent, str) or not AGENT_NAME.match(agent):
        raise Reply(400, 'agent_name: 1-64 of A-Z a-z 0-9 . _ -')

    if running_workers() >= MAX_WORKERS:
        raise Reply(
            429, f'all {MAX_WORKERS} workers are busy; try again later'
        )
    take_quota(key)

    job = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)
    created = now()
    pred = {
        'instance_id': instance,
        'model_name_or_path': agent,
        'model_patch': patch,
    }
    S3.put_object(
        Bucket=BUCKET,
        Key=f'patches/{job}.pred',
        Body=json.dumps(pred).encode(),
        ContentType='application/json',
    )
    TABLE.put_item(
        Item={
            'id': job,
            'api_key_id': key,
            'instance_id': instance,
            'agent_name': agent,
            'status': 'QUEUED',
            'created_at': iso(created),
            'token_hash': digest(token),
            'harness_commit': HARNESS_COMMIT,
            'expires_at': int(created.timestamp()) + 30 * 86400,
        }
    )
    context = event['requestContext']
    api = f'https://{context["domainName"]}/{context["stage"]}'
    try:
        launched = EC2.run_instances(
            LaunchTemplate={
                'LaunchTemplateId': LAUNCH_TEMPLATE_ID,
                'Version': '$Latest',
            },
            MinCount=1,
            MaxCount=1,
            UserData=render_user_data(api, job, token, instance),
            TagSpecifications=[
                {
                    'ResourceType': kind,
                    'Tags': [
                        {'Key': WORKER_TAG, 'Value': 'worker'},
                        {'Key': JOB_TAG, 'Value': job},
                        {'Key': 'Name', 'Value': f'sbmdt-api-{job[:8]}'},
                    ],
                }
                for kind in ('instance', 'volume')
            ],
        )
    except ClientError as error:
        finish(job, 'FAILED', error=f'could not start a worker: {error}')
        refund_quota(key)
        raise Reply(503, 'could not start a worker; try again later') from None
    ec2_id = launched['Instances'][0]['InstanceId']
    TABLE.update_item(
        Key={'id': job},
        UpdateExpression='SET #s = :s, ec2_instance_id = :e',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':s': 'RUNNING', ':e': ec2_id},
    )
    return {
        '_status': 202,
        'id': job,
        'instance_id': instance,
        'agent_name': agent,
        'status': 'RUNNING',
        'created_at': iso(created),
    }


def status(event: dict[str, Any]) -> dict[str, Any]:
    key = api_key_id(event)
    job = (event.get('pathParameters') or {}).get('id', '')
    item = load_job(job)
    if item is None or item.get('api_key_id') != key:
        raise Reply(404, 'no such evaluation')
    if item['status'] not in FINISHED and stale(item):
        terminate(item.get('ec2_instance_id'))
        finish(job, 'TIMEOUT', error=f'no result after {STALE_MINUTES} min')
        item = load_job(job) or item
    out = {
        k: item.get(k)
        for k in (
            'id',
            'instance_id',
            'agent_name',
            'status',
            'created_at',
            'finished_at',
            'error',
            'harness_commit',
        )
        if item.get(k) is not None
    }
    if item['status'] == 'DONE':
        try:
            obj = S3.get_object(Bucket=BUCKET, Key=f'results/{job}.json')
            out['result'] = json.loads(obj['Body'].read())
        except ClientError:
            out['result'] = None
    return out


def instances(event: dict[str, Any]) -> dict[str, Any]:
    api_key_id(event)
    return {'instances': sorted(INSTANCES)}


# ---------------------------------------------------------------- workers


def worker_job(event: dict[str, Any]) -> dict[str, Any]:
    job = (event.get('pathParameters') or {}).get('id', '')
    token = (event.get('headers') or {}).get('X-Job-Token') or (
        event.get('headers') or {}
    ).get('x-job-token')
    item = load_job(job)
    if (
        item is None
        or not token
        or 'token_hash' not in item
        or not secrets.compare_digest(item['token_hash'], digest(token))
    ):
        raise Reply(403, 'bad job token')
    return item


def worker_patch(event: dict[str, Any]) -> dict[str, Any]:
    item = worker_job(event)
    obj = S3.get_object(Bucket=BUCKET, Key=f'patches/{item["id"]}.pred')
    return json.loads(obj['Body'].read())


def worker_result(event: dict[str, Any]) -> dict[str, Any]:
    item = worker_job(event)
    data = body_json(event, 4 << 20)
    state = data.get('status')
    if state not in ('DONE', 'FAILED'):
        raise Reply(400, 'status must be DONE or FAILED')
    job = item['id']
    if data.get('log'):
        S3.put_object(
            Bucket=BUCKET,
            Key=f'results/{job}.log',
            Body=str(data['log']).encode(),
            ContentType='text/plain',
        )
    verdict = data.get('verdict') if state == 'DONE' else None
    if verdict is not None:
        S3.put_object(
            Bucket=BUCKET,
            Key=f'results/{job}.json',
            Body=json.dumps(verdict).encode(),
            ContentType='application/json',
        )
    finish(
        job,
        state,
        error=None if state == 'DONE' else str(data.get('error', ''))[:2000],
        resolved=None if verdict is None else bool(verdict.get('resolved')),
    )
    return {'ok': True}


# ---------------------------------------------------------------- helpers


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load_job(job: str) -> dict[str, Any] | None:
    if not JOB_ID.match(job or ''):
        return None
    return TABLE.get_item(Key={'id': job}).get('Item')


def finish(job: str, state: str, error: str | None = None, **extra) -> None:
    """Record a job's end; its token stops working."""
    names = {'#s': 'status'}
    values: dict[str, Any] = {':s': state, ':f': iso(now())}
    sets = ['#s = :s', 'finished_at = :f']
    if error:
        sets.append('#e = :e')
        names['#e'] = 'error'
        values[':e'] = error
    for name, value in extra.items():
        if value is not None:
            sets.append(f'{name} = :{name}')
            values[f':{name}'] = value
    TABLE.update_item(
        Key={'id': job},
        UpdateExpression='SET ' + ', '.join(sets) + ' REMOVE token_hash',
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def stale(item: dict[str, Any]) -> bool:
    created = dt.datetime.fromisoformat(item['created_at'])
    return now() - created > dt.timedelta(minutes=STALE_MINUTES)


def terminate(ec2_id: str | None) -> None:
    if not ec2_id:
        return
    try:
        EC2.terminate_instances(InstanceIds=[ec2_id])
    except ClientError:
        pass  # already gone, or not one of ours (IAM refuses those)


def running_workers() -> int:
    """Count live workers, terminating any that outlived STALE_MINUTES."""
    pages = EC2.get_paginator('describe_instances').paginate(
        Filters=[
            {'Name': f'tag:{WORKER_TAG}', 'Values': ['worker']},
            {'Name': 'instance-state-name', 'Values': ['pending', 'running']},
        ]
    )
    live = 0
    cutoff = now() - dt.timedelta(minutes=STALE_MINUTES)
    for page in pages:
        for reservation in page['Reservations']:
            for instance in reservation['Instances']:
                if instance['LaunchTime'] < cutoff:
                    terminate(instance['InstanceId'])
                    tags = {t['Key']: t['Value'] for t in instance['Tags']}
                    job = tags.get(JOB_TAG)
                    item = load_job(job) if job else None
                    if item and item['status'] not in FINISHED:
                        finish(job, 'TIMEOUT', error='worker outlived its run')
                else:
                    live += 1
    return live


def take_quota(key: str) -> None:
    day = now().strftime('%Y-%m-%d')
    try:
        TABLE.update_item(
            Key={'id': f'quota#{key}#{day}'},
            UpdateExpression='ADD used :one SET expires_at = :exp',
            ConditionExpression='attribute_not_exists(used) OR used < :limit',
            ExpressionAttributeValues={
                ':one': 1,
                ':limit': DAILY_LIMIT,
                ':exp': int(time.time()) + 3 * 86400,
            },
        )
    except ClientError as error:
        code = error.response['Error']['Code']
        if code == 'ConditionalCheckFailedException':
            raise Reply(
                429, f'daily limit of {DAILY_LIMIT} evaluations reached'
            ) from None
        raise


def refund_quota(key: str) -> None:
    day = now().strftime('%Y-%m-%d')
    TABLE.update_item(
        Key={'id': f'quota#{key}#{day}'},
        UpdateExpression='ADD used :minus',
        ExpressionAttributeValues={':minus': -1},
    )


def enabled() -> bool:
    """The kill switch: the SSM parameter must read ``true``."""
    global _enabled_cache
    checked, value = _enabled_cache
    if time.time() - checked > 30:
        try:
            raw = SSM.get_parameter(Name=ENABLED_PARAMETER)['Parameter']
            value = raw['Value'].strip().lower() == 'true'
        except ClientError:
            value = False
        _enabled_cache = (time.time(), value)
    return value


def render_user_data(api: str, job: str, token: str, instance: str) -> str:
    script = USER_DATA
    for name, value in {
        '__API__': api,
        '__JOB__': job,
        '__TOKEN__': token,
        '__INSTANCE__': instance,
        '__COMMIT__': HARNESS_COMMIT,
        '__RUN_MINUTES__': str(RUN_MINUTES),
        '__TEST_MINUTES__': str(max(RUN_MINUTES - 15, 5)),
    }.items():
        script = script.replace(name, value)
    return base64.b64encode(script.encode()).decode()
