"""Launch a short-lived EC2 instance, run a command on it via SSM, then
terminate it.

Used to run the pipeline on distributed cloud computing instances built from
``IMAGE_ID``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import shlex
import signal
import time
from collections.abc import Coroutine
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError, WaiterError
from mypy_boto3_ec2 import EC2Client
from mypy_boto3_ssm import SSMClient

from sbmdt.evaluator.base import PatchType
from sbmdt.log import setup_logging
from sbmdt.s3 import (
    PREDS_S3_BUCKET_NAME,
    STDOUT_S3_BUCKET_NAME,
    TEST_RESULTS_S3_BUCKET_NAME,
    S3PredFilename,
    get_all_keys_in_s3_bucket,
)

N_CONCURRENT: Final[int] = 5
"""Maximum number of EC2 instances allowed to be running (i.e. mid-evaluation)
at the same time; enforced via the semaphore in `main()`.
"""

_shutdown = asyncio.Event()
"""Set by the SIGINT/SIGTERM handler.

``main()`` watches for it to stop waiting on the rest of the batch and start
terminating in-flight instances; ``run_instance_async`` checks it too, so
tasks still waiting on the semaphore skip creating a new instance once it's
set.
"""


class TaskContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            task = asyncio.current_task()
            record.taskName = task.get_name() if task else 'main'
        except RuntimeError:
            record.taskName = 'main'
        return True


log = logging.getLogger(__name__)


def setup_logging_for_asyncio() -> None:
    log.addFilter(TaskContextFilter())
    for handler in log.handlers:
        formatter = handler.formatter
        if formatter is None:
            continue
        fmt = formatter._fmt
        if fmt is None:
            continue
        new_format = fmt.replace(
            '%(message)s',
            '[%(threadName)s:%(thread)d:%(taskName)s] %(message)s',
        )
        formatter._fmt = new_format


IMAGE_ID = 'ami-0412980e86e47df68'
INSTANCE_TYPE = 't2.medium'
SUBNET_ID = 'subnet-047914ca11e3cc7b5'
SECURITY_GROUP_ID = 'sg-0efcda98c4f731b8b'
INSTANCE_PROFILE_ARN = (
    'arn:aws:iam::607869540801:instance-profile/sbmdt-instance-profile'
)
REGION = 'us-east-1'
BLOCK_DEVICE_NAME = '/dev/xvda'
BLOCK_VOLUME_SIZE_GB = 8

AUTO_SHUTDOWN_SCRIPT = """#!/bin/bash
shutdown -h +30
"""

_cleanup_state: list[tuple[EC2Client, str]] = []
"""(ec2 client, instance_id) pairs for instances that have been created.

Each concurrently running ``run_instance`` call appends its own entry once
its instance exists, so ``_terminate_known_instances`` can drain this list
and terminate every outstanding instance on SIGINT/SIGTERM, not just the
most recently created one. Entries are not removed when an instance terminates
normally, so this grows for the lifetime of the process; terminating an
already-terminated instance again from a stale entry is expected to be a
harmless no-op.
"""


def create_instance(ec2: EC2Client, instance_name: str) -> str:
    """Launch a single EC2 instance and return its instance ID.

    Args:
        ec2: EC2 client used to issue the ``run_instances`` request.
        instance_name: Value to set as the instance's ``Name`` tag.

    Returns:
        The ID of the newly created instance.

    Raises:
        Exception: If the response does not contain an instance ID.
    """
    response = ec2.run_instances(
        ImageId=IMAGE_ID,
        InstanceType=INSTANCE_TYPE,
        MinCount=1,
        MaxCount=1,
        SubnetId=SUBNET_ID,
        SecurityGroupIds=[SECURITY_GROUP_ID],
        IamInstanceProfile={'Arn': INSTANCE_PROFILE_ARN},
        # Terminate on shutdown
        InstanceInitiatedShutdownBehavior='terminate',
        # Shutdown after 30 minutes automatically as a protection measure in
        # case the terminate script fails to terminate the instance
        UserData=AUTO_SHUTDOWN_SCRIPT,
        # Make a custom hard drive size
        BlockDeviceMappings=[
            {
                'DeviceName': BLOCK_DEVICE_NAME,
                'Ebs': {
                    'VolumeSize': BLOCK_VOLUME_SIZE_GB,
                    'VolumeType': 'gp3',
                    'DeleteOnTermination': True,
                },
            }
        ],
        TagSpecifications=[
            {
                'ResourceType': 'instance',
                'Tags': [{'Key': 'Name', 'Value': instance_name}],
            }
        ],
    )

    # MinCount/MaxCount above are both 1, so exactly one instance is
    # expected in the response.
    instance_id = response.get('Instances', [{}])[0].get('InstanceId', None)
    if instance_id is None:
        raise Exception('no instance id')

    return instance_id


def wait_for_instance(ec2: EC2Client, instance_id: str) -> None:
    """Block until the given instance reaches the "running" state."""
    ec2.get_waiter('instance_running').wait(InstanceIds=[instance_id])


def terminate_instance(
    ec2: EC2Client, instance_id: str, max_attempts: int = 5
) -> None:
    """Terminate the given instance and block until it is fully terminated.
    Retries the terminate call itself on transient AWS errors.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            log.info(f'Attempting to terminate EC2 instance {instance_id}')
            ec2.terminate_instances(InstanceIds=[instance_id])
            break
        except ClientError as e:
            log.error(f'Terminate attempt {attempt} failed: {e}')
            if attempt == max_attempts:
                raise
            time.sleep(2**attempt)

    try:
        log.info(f'Waiting for EC2 instance {instance_id} to terminate')
        ec2.get_waiter('instance_terminated').wait(InstanceIds=[instance_id])
        log.info(f'{instance_id} terminated')
    except WaiterError as e:
        # Terminate request was accepted; waiter failing doesn't mean the
        # instance is still running. Log and don't mask it as total failure.
        log.error(f'Waiter for termination failed, verify manually: {e}')


def wait_for_ssm(
    ssm: SSMClient, instance_id: str, timeout_s: int = 300
) -> None:
    """Poll SSM until the instance registers as managed, or time out.

    A running EC2 instance is not immediately controllable via SSM: the
    SSM agent needs time to start and check in. This polls
    ``describe_instance_information`` every 5 seconds until the instance
    shows up.

    Args:
        ssm: SSM client used to check instance registration.
        instance_id: ID of the instance to wait for.
        timeout_s: Maximum number of seconds to wait before giving up.

    Raises:
        TimeoutError: If the instance has not registered within
            ``timeout_s`` seconds.
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        resp = ssm.describe_instance_information(
            Filters=[{'Key': 'InstanceIds', 'Values': [instance_id]}]
        )
        if resp['InstanceInformationList']:
            return
        time.sleep(5)
    raise TimeoutError(f'{instance_id} did not register with SSM in time')


def send_ssm_command(
    ssm: SSMClient, instance_id: str, commands: list[str]
) -> str:
    """Run shell commands on an instance via SSM and return the stdout.

    Sends the commands as an ``AWS-RunShellScript`` document, blocks until
    the command finishes, and logs the failure details (status and
    stderr) before re-raising if the command does not complete
    successfully.

    Args:
        ssm: SSM client used to send and track the command.
        instance_id: ID of the instance to run the commands on.
        commands: Shell commands to execute on the instance.

    Returns:
        The captured standard output of the command.

    Raises:
        Exception: If the response does not contain a command ID.
        WaiterError: If the command does not complete successfully.
    """
    log.info(f'Running commands {" ; ".join(commands)}')
    send = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName='AWS-RunShellScript',
        Parameters={'commands': commands},
    )

    command_id = send.get('Command', {}).get('CommandId', None)
    if command_id is None:
        raise Exception('no command id')

    log.info(f'Waiting for command ID {command_id}')
    waiter = ssm.get_waiter('command_executed')

    try:
        waiter.wait(CommandId=command_id, InstanceId=instance_id)
    except WaiterError:
        result = ssm.get_command_invocation(
            CommandId=command_id, InstanceId=instance_id
        )
        log.error(
            f'Command failed. Status: {result.get("Status")}, '
            f'StatusDetails: {result.get("StatusDetails")}, '
            f'Stderr: {result.get("StandardErrorContent")}'
        )

    log.info(f'Getting command invocation for command ID {command_id}')
    result = ssm.get_command_invocation(
        CommandId=command_id, InstanceId=instance_id
    )
    return result['StandardOutputContent']


def make_command(
    sbmdt_instance_id: str, patch_type: PatchType, pred_s3_key: str
) -> str:
    """Build the shell command to run on the EC2 instance via SSM.

    Wraps ``aws/run_ec2.sh`` (invoked from ``/opt/sbmdt``, which is expected
    to exist on the AMI referenced by ``IMAGE_ID``) with flags:

        --instance-id     ``sbmdt_instance_id`` -- benchmark instance ID to
            evaluate.
        --patch-type      ``patch_type`` -- patch state to evaluate under.
        --pred-bucket     ``PREDS_S3_BUCKET_NAME`` -- bucket containing the
            input prediction file.
        --pred-key        ``pred_s3_key`` -- key of the input prediction
            file within that bucket.
        --results-bucket  ``TEST_RESULTS_S3_BUCKET_NAME`` -- bucket the
            evaluation results should be uploaded to.
        --stdout-bucket   ``STDOUT_S3_BUCKET_NAME`` -- bucket the command's
            stdout/log should be uploaded to.
        --stdout-key      ``stdout_s3_key`` -- key the command's
            stdout/log should be uploaded to within that bucket (derived
            from ``pred_s3_key`` by swapping the ``.pred`` extension for
            ``.log``).

    Args:
        sbmdt_instance_id: Benchmark instance ID to evaluate.
        patch_type: Patch state to evaluate under.
        pred_s3_key: S3 key of the prediction file to evaluate, within
            ``PREDS_S3_BUCKET_NAME``.

    Returns:
        The full shell command string to execute on the instance.
    """
    stdout_s3_key = S3PredFilename.decode(pred_s3_key).encode(extension='.log')
    args = [
        'bash',
        'aws/run_ec2.sh',
        '--instance-id',
        sbmdt_instance_id,
        '--patch-type',
        str(patch_type),
        '--pred-bucket',
        PREDS_S3_BUCKET_NAME,
        '--pred-key',
        pred_s3_key,
        '--results-bucket',
        TEST_RESULTS_S3_BUCKET_NAME,
        '--stdout-bucket',
        STDOUT_S3_BUCKET_NAME,
        '--stdout-key',
        stdout_s3_key,
    ]
    command = 'cd /opt/sbmdt && ' + shlex.join(args)
    return command


def run_instance(
    sbmdt_instance_id: str, patch_type: PatchType, pred_s3_key: str
) -> None:
    """Create an EC2 instance, run a single evaluation command on it via
    SSM, then tear it down.

    Blocking; intended to be run off the event loop via
    ``run_instance_async``.
    """

    now = dt.datetime.now(tz=dt.UTC)
    # Timestamp suffix keeps instance names unique across runs.
    instance_name = f'sbmdt-ec2-{now.timestamp()}'

    log.info('Starting session')
    # Uses the local AWS CLI profile named "admin-user" rather than the
    # default credential chain.
    session = boto3.Session(profile_name='admin-user')
    ec2 = session.client('ec2', region_name=REGION)

    # Once the instance exists, always terminate it on the way out, even if
    # waiting for SSM, sending the command, or anything else below raises.
    instance_id = None
    try:
        log.info('Creating instance')
        instance_id = create_instance(ec2, instance_name)
        log.info(f'Created instance: {instance_id}')
        _cleanup_state.append((ec2, instance_id))

        log.info('Waiting for instance to become ready')
        wait_for_instance(ec2, instance_id)

        ssm = session.client('ssm', region_name=REGION)

        log.info('Waiting for SSM')
        wait_for_ssm(ssm, instance_id)

        log.info('Sending command')
        output = send_ssm_command(
            ssm,
            instance_id,
            [make_command(sbmdt_instance_id, patch_type, pred_s3_key)],
        )
        log.info(f'Received output: {output}')
    finally:
        if instance_id is not None:
            log.info('Terminating instance')
            terminate_instance(ec2, instance_id)

    log.info('Done')


async def run_instance_async(
    sbmdt_instance_id: str,
    patch_type: PatchType,
    pred_s3_key: str,
    sem: asyncio.Semaphore,
) -> None:
    """Run ``run_instance`` in a worker thread, bounded by ``sem``.

    ``run_instance`` is synchronous (it blocks on boto3 waiters), so it is
    offloaded to a thread via ``asyncio.to_thread`` to let up to
    ``N_CONCURRENT`` instances run concurrently without blocking the event
    loop.

    If ``_shutdown`` is already set by the time this task acquires ``sem``,
    it returns without creating an instance -- there's no way to interrupt
    ``run_instance`` once it's running in its thread, so this only prevents
    starting *new* work after a shutdown was requested.
    """
    async with sem:
        if _shutdown.is_set():
            log.warning(
                f'Coroutine for {sbmdt_instance_id} {patch_type} with pred '
                f'{pred_s3_key} received shutdown signal, aborting'
            )
            return
        log.info(
            f'Running {sbmdt_instance_id} {patch_type} with pred {pred_s3_key}'
        )
        return await asyncio.to_thread(
            run_instance, sbmdt_instance_id, patch_type, pred_s3_key
        )


def _request_shutdown(signum: int) -> None:
    """Sets the shutdown signal."""
    log.warning(f'Received signal {signum}, shutting down gracefully')
    _shutdown.set()
    log.warning('Shutdown signal has been set')


async def _terminate_known_instances() -> None:
    """Terminates every instance currently tracked in ``_cleanup_state``.

    A single drain pass: instances created after this call starts (e.g. a
    task still waiting on the semaphore when shutdown was requested) aren't
    covered by it.
    """
    coros: list[Coroutine[Any, Any, None]] = []
    while _cleanup_state:
        ec2, instance_id = _cleanup_state.pop()
        log.warning(f'Adding EC2 instance {instance_id} to terminate queue')
        coros.append(asyncio.to_thread(terminate_instance, ec2, instance_id))
    await asyncio.gather(*coros, return_exceptions=True)


async def main() -> None:
    """Evaluate every prediction currently in ``PREDS_S3_BUCKET_NAME``.

    Launches one EC2 instance per prediction file found in the bucket (up
    to ``N_CONCURRENT`` at a time), shuffled so that a single unlucky batch
    of slow/large instances isn't processed all at once.

    Registers the SIGINT/SIGTERM handler on this coroutine's running loop
    (``asyncio.run`` creates a fresh loop per call, so this can't be done
    beforehand). If a shutdown is requested before all instances finish,
    terminates every currently-tracked instance and waits for the
    already-running work to unwind before returning.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _request_shutdown, sig)

    pred_s3_keys = get_all_keys_in_s3_bucket(PREDS_S3_BUCKET_NAME)
    tasks: list[Coroutine[Any, Any, None]] = []
    sem = asyncio.Semaphore(N_CONCURRENT)
    for key in pred_s3_keys:
        pred_filename = S3PredFilename.decode(key)
        sbmdt_instance_id = pred_filename.instance_id
        patch_type = pred_filename.patch_type
        tasks.append(
            run_instance_async(sbmdt_instance_id, patch_type, key, sem)
        )

    random.shuffle(tasks)

    work_tasks = asyncio.gather(*tasks, return_exceptions=True)
    shutdown_wait = asyncio.create_task(_shutdown.wait())

    # Race work against the shutdown signal so we react as soon as either
    # finishes, rather than always waiting for the full batch.
    await asyncio.wait(
        (work_tasks, shutdown_wait), return_when=asyncio.FIRST_COMPLETED
    )

    # If shutdown was requested before all work finished, terminate every
    # currently-tracked instance so in-flight run_instance calls fail fast
    # instead of running their full multi-minute lifecycle. Those failures
    # are captured (not raised) by work_tasks's return_exceptions=True.
    if shutdown_wait.done():
        if work_tasks.done():
            log.warning('Shutdown requested, but work had already finished')
        else:
            log.warning(
                'Shutdown requested with work still pending, terminating '
                'known instances now'
            )
            await _terminate_known_instances()
            await work_tasks
    else:
        log.info('Work finished without a shutdown request')

    log.info('Done!')


if __name__ == '__main__':
    setup_logging(level=logging.INFO)
    setup_logging_for_asyncio()
    asyncio.run(main())
