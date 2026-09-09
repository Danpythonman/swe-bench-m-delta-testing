"""Helpers for running shell commands on EC2 instances via AWS SSM.

Wraps the ``boto3`` SSM client's blocking calls in ``run_in_executor`` so
they can be awaited, and provides polling utilities for instance
registration and command completion.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Final

from botocore.exceptions import (
    ClientError,
    ConnectionError,
    EndpointConnectionError,
)
from mypy_boto3_ssm import SSMClient
from mypy_boto3_ssm.type_defs import (
    GetCommandInvocationResultTypeDef,
    SendCommandResultTypeDef,
)

__all__ = [
    'wait_for_ssm',
    'start_running_ssm_command',
    'get_ssm_command_invocation',
    'send_ssm_command',
]

log = logging.getLogger(__name__)

# Default time limit for a single instance evaluation, overridable per
# call via the `timeout_seconds` argument to `send_ssm_command`.
#
# Two separate limits bound a command, and the shorter one silently wins:
# how long SSM lets it run on the instance (the `executionTimeout`
# parameter to AWS-RunShellScript, which defaults to 3600 when unset and
# so must be passed explicitly), and how long we poll for it to finish.
# Both derive from this one value.
#
# Sized from the 1,072 runs already in S3: median 6.7 min, p90 22.0 min,
# p99 29.7 min, max 65.8 min. The previous 30-minute poll cut off the
# slowest 0.6% of runs, and the unset 1-hour execution timeout cut off
# the slowest run. 90 minutes clears the observed maximum with headroom.
DEFAULT_TIMEOUT_MINUTES: Final[int] = 90
EXECUTION_TIMEOUT_SECONDS: Final[int] = DEFAULT_TIMEOUT_MINUTES * 60
POLL_INTERVAL_SECONDS: Final[int] = 10


# A network blip between this machine and the AWS API is a different
# failure than anything AWS-side: the EC2 instance already exists and is
# already being paid for, so losing the whole evaluation to a one-off
# connection drop is wasteful in a way retrying a few seconds later is
# not. Kept separate from get_ssm_command_invocation's existing retry,
# which is for an AWS-side eventual-consistency race, not a transport
# failure.
TRANSIENT_CONNECTION_RETRIES: Final[int] = 4
TRANSIENT_CONNECTION_DELAY_SECONDS: Final[float] = 3.0


async def _call_with_connection_retry(
    call: 'Callable[[], object]',
    description: str,
    max_retries: int = TRANSIENT_CONNECTION_RETRIES,
    retry_delay_seconds: float = TRANSIENT_CONNECTION_DELAY_SECONDS,
) -> object:
    """Run a blocking boto3 call, retrying on a transient connection drop.

    Args:
        call: Zero-argument callable making the boto3 request.
        description: Short label for what is being called, used in the
            retry log line.
        max_retries: Maximum number of attempts before giving up.
        retry_delay_seconds: Delay between retries, in seconds.

    Returns:
        Whatever ``call`` returns.

    Raises:
        EndpointConnectionError: If every attempt fails to connect.
        ConnectionError: If every attempt fails to connect.
    """
    loop = asyncio.get_running_loop()
    for attempt in range(1, max_retries + 1):
        try:
            return await loop.run_in_executor(None, call)
        except (EndpointConnectionError, ConnectionError):
            if attempt == max_retries:
                raise
            log.info(
                f'{description} could not connect (attempt '
                f'{attempt}/{max_retries}), retrying in '
                f'{retry_delay_seconds}s...'
            )
            await asyncio.sleep(retry_delay_seconds)
    raise Exception('connection retries exceeded')


async def wait_for_ssm(
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
        resp = await _call_with_connection_retry(
            lambda: ssm.describe_instance_information(
                Filters=[{'Key': 'InstanceIds', 'Values': [instance_id]}]
            ),
            'describe_instance_information',
        )
        if resp['InstanceInformationList']:
            return
        await asyncio.sleep(5)
    raise TimeoutError(f'{instance_id} did not register with SSM in time')


async def start_running_ssm_command(
    ssm: SSMClient,
    instance_id: str,
    command: str,
    timeout_seconds: int = EXECUTION_TIMEOUT_SECONDS,
) -> SendCommandResultTypeDef:
    """Send a shell command to an instance via SSM without waiting for it.

    Args:
        ssm: SSM client used to send the command.
        instance_id: ID of the instance to run the command on.
        command: Shell command to execute on the instance.
        timeout_seconds: How long SSM lets the command run on the
            instance before killing it. Defaults to
            :data:`EXECUTION_TIMEOUT_SECONDS`.

    Returns:
        The raw ``send_command`` response, including the command ID needed
        to poll for its result.
    """
    send = await _call_with_connection_retry(
        lambda: ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName='AWS-RunShellScript',
            Parameters={
                'commands': [command],
                # Without this, SSM applies its own 3600s default and
                # kills long evaluations regardless of how long we wait.
                'executionTimeout': [str(timeout_seconds)],
            },
        ),
        'send_command',
    )
    return send


async def get_ssm_command_invocation(
    ssm: SSMClient,
    instance_id: str,
    command_id: str,
    max_retries: int = 5,
    retry_delay_seconds: float = 2.0,
) -> GetCommandInvocationResultTypeDef:
    """Fetch the invocation status for a previously sent SSM command.

    Immediately after ``send_command`` returns, the invocation may not yet
    be queryable, so ``InvocationDoesNotExist`` errors are retried with a
    delay rather than treated as failures.

    Args:
        ssm: SSM client used to query the command invocation.
        instance_id: ID of the instance the command was run on.
        command_id: ID of the command to look up.
        max_retries: Maximum number of attempts before giving up.
        retry_delay_seconds: Delay between retries, in seconds.

    Returns:
        The command invocation details, including status and output.

    Raises:
        ClientError: If the invocation lookup fails for a reason other
            than the invocation not existing yet, or if it still does not
            exist after ``max_retries`` attempts.
    """
    for attempt in range(1, max_retries + 1):
        try:
            log.info(f'Getting command invocation {command_id}')
            return await _call_with_connection_retry(
                lambda: ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=instance_id
                ),
                'get_command_invocation',
            )
        except ClientError as e:
            if (
                e.response.get('Error', {}).get('Code')
                != 'InvocationDoesNotExist'
            ):
                raise
            if attempt == max_retries:
                raise
            log.info(
                f'Invocation for command {command_id} not yet available '
                f'(attempt {attempt}/{max_retries}), retrying...'
            )
            await asyncio.sleep(retry_delay_seconds)
    raise Exception('command retries exceeded')


async def send_ssm_command(
    ssm: SSMClient,
    instance_id: str,
    command: str,
    timeout_seconds: int = EXECUTION_TIMEOUT_SECONDS,
) -> str:
    """Run shell commands on an instance via SSM and return the stdout.

    Sends the commands as an ``AWS-RunShellScript`` document, blocks until
    the command finishes, and logs the failure details (status and
    stderr) before re-raising if the command does not complete
    successfully.

    ``timeout_seconds`` drives both limits that bound a command: how long
    SSM lets it run on the instance, and how long this function waits for
    it. Passing one value keeps the two from drifting apart, since the
    shorter of them silently wins.

    Args:
        ssm: SSM client used to send and track the command.
        instance_id: ID of the instance to run the commands on.
        command: Shell commands to execute on the instance.
        timeout_seconds: How long the command may run before SSM kills it
            and this function gives up. Defaults to
            :data:`EXECUTION_TIMEOUT_SECONDS`.

    Returns:
        The captured standard output of the command.

    Raises:
        Exception: If the response does not contain a command ID.
        WaiterError: If the command does not complete successfully.
    """
    log.info(f'Running command {command}')
    send = await start_running_ssm_command(
        ssm, instance_id, command, timeout_seconds=timeout_seconds
    )

    command_id = send.get('Command', {}).get('CommandId', None)
    if command_id is None:
        raise Exception('no command id')

    log.info(f'Waiting for command ID {command_id}')

    poll_interval_seconds = POLL_INTERVAL_SECONDS
    terminal_statuses = {'Success', 'Failed', 'Cancelled', 'TimedOut'}
    deadline = time.monotonic() + timeout_seconds

    result = None
    while time.monotonic() < deadline:
        result = await get_ssm_command_invocation(ssm, instance_id, command_id)
        status = result.get('Status')
        log.info(f'Got command invocation {command_id}, status: {status}')
        if status in terminal_statuses:
            break
        await asyncio.sleep(poll_interval_seconds)
    else:
        log.error(
            f'Command timed out after {timeout_seconds}s. Command: {command}'
        )
        raise Exception(
            f'Command {command_id} timed out after {timeout_seconds}s'
        )

    if status != 'Success':
        log.error(
            f'Command failed.'
            f'\n\tCommand: {command}'
            f'\n\tStatus: {result.get("Status")}, '
            f'\n\tStatusDetails: {result.get("StatusDetails")}, '
            f'\n\tStdout: {result.get("StandardOutputContent")}, '
            f'\n\tStderr: {result.get("StandardErrorContent")}'
        )
        raise Exception(
            f'Command {command_id} failed with status {result.get("Status")}'
        )
    else:
        log.info(f'Command success {command_id}')

    return result['StandardOutputContent']
