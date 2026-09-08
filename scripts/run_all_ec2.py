"""Launch a short-lived EC2 instance, run a command on it via SSM, then
terminate it.

Used to run the pipeline on distributed cloud computing instances built from
``IMAGE_ID``.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import random
import shlex
import signal
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, get_args

import boto3
from mypy_boto3_ec2 import EC2Client
from mypy_boto3_ec2.literals import InstanceTypeType

from sbmdt.aws.ec2 import (
    create_instance,
    terminate_instance,
    wait_for_instance,
)
from sbmdt.aws.env import (
    AWS_PROFILE,
    BLOCK_DEVICE_NAME,
    BLOCK_VOLUME_SIZE_GB,
    IMAGE_ID,
    INSTANCE_PROFILE_ARN,
    INSTANCE_TYPE,
    REGION,
    SECURITY_GROUP_ID,
    SUBNET_ID,
)
from sbmdt.aws.s3 import (
    PREDS_S3_BUCKET_NAME,
    STDOUT_S3_BUCKET_NAME,
    TEST_RESULTS_S3_BUCKET_NAME,
    S3PredFilename,
    get_all_keys_in_s3_bucket,
)
from sbmdt.aws.ssm import (
    DEFAULT_TIMEOUT_MINUTES,
    send_ssm_command,
    wait_for_ssm,
)
from sbmdt.evaluator.base import PatchType
from sbmdt.log import setup_logging, setup_logging_for_asyncio

_shutdown = asyncio.Event()
"""Set by the SIGINT/SIGTERM handler.

``main()`` watches for it to stop waiting on the rest of the batch and start
terminating in-flight instances; ``run_instance_async`` checks it too, so
tasks still waiting on the semaphore skip creating a new instance once it's
set.
"""


log = logging.getLogger(__name__)


@dataclass(kw_only=True)
class RunArgs:
    pred_keys: list[str] | None
    n_concurrent: int
    image_id: str
    instance_type: InstanceTypeType
    subnet_id: str
    security_group_id: str
    instance_profile_arn: str
    region: str
    block_device_name: str
    block_volume_size_gb: int
    aws_profile: str
    git_branch: str | None
    apply_test_patch: bool
    run_timeout_minutes: int


N_CONCURRENT = 5
"""Maximum number of EC2 instances allowed to be running (i.e. mid-evaluation)
at the same time; enforced via the semaphore in `main()`.

Overridden by ``--n-concurrent`` when run as a script.
"""

"""Local AWS CLI profile used to create the boto3 session in
``run_instance``, rather than the default credential chain.
"""
GIT_BRANCH: str | None = None
"""If set, checked out on the instance (via ``make_git_checkout_command``)
before the evaluation command is run.
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


def make_command(
    sbmdt_instance_id: str,
    patch_type: PatchType,
    pred_s3_key: str,
    apply_test_patch: bool = False,
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
        --apply-test-patch  present when ``apply_test_patch`` is set;
            applies the instance's test patch on top of the model
            patch so the maintainer's FAIL_TO_PASS tests are present.
        --stdout-key      ``stdout_s3_key`` -- key the command's
            stdout/log should be uploaded to within that bucket (derived
            from ``pred_s3_key`` by swapping the ``.pred`` extension for
            ``.log``).

    Args:
        sbmdt_instance_id: Benchmark instance ID to evaluate.
        patch_type: Patch state to evaluate under.
        pred_s3_key: S3 key of the prediction file to evaluate, within
            ``PREDS_S3_BUCKET_NAME``.
        apply_test_patch: Whether to pass ``--apply-test-patch``
            through to ``run_instance.py``.

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
    if apply_test_patch:
        args.append('--apply-test-patch')
    command = 'cd /opt/sbmdt && ' + shlex.join(args)
    return command


def make_git_checkout_command(branch: str) -> str:
    """Build the shell command to fetch and check out ``branch`` in
    ``/opt/sbmdt`` on the EC2 instance, ahead of running the evaluation
    command built by ``make_command``.

    Three details of the environment have to be worked around. The
    repository is cloned as ``ssm-user`` at image build time but SSM runs
    commands as root, so git refuses to touch it as "dubious ownership"
    until the directory is marked safe. That setting cannot be written
    with ``git config --global``, because SSM also runs without ``HOME``
    and git cannot locate a config file to write (``fatal: $HOME not
    set``), so it is passed inline with ``-c`` instead, which applies to
    the single invocation and leaves nothing behind. Finally, the clone
    already has a local branch for whatever the image was built from, so
    a plain ``checkout -b`` fails when asked for that same branch; ``-B``
    resets it to the fetched ref instead.

    Args:
        branch: Name of the git branch to fetch and check out.

    Returns:
        The full shell command string to execute on the instance.
    """
    ref = f'refs/remotes/origin/{branch}'
    git = ['git', '-c', 'safe.directory=/opt/sbmdt']
    fetch = [*git, 'fetch', '--depth', '1', 'origin', f'+{branch}:{ref}']
    checkout = [*git, 'checkout', '-B', branch, f'origin/{branch}']
    command = ' && '.join(
        ['cd /opt/sbmdt', shlex.join(fetch), shlex.join(checkout)]
    )
    return command


async def run_instance(
    sbmdt_instance_id: str,
    patch_type: PatchType,
    pred_s3_key: str,
    run_args: RunArgs,
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
    session = boto3.Session(profile_name=run_args.aws_profile)
    ec2 = session.client('ec2', region_name=run_args.region)

    # Once the instance exists, always terminate it on the way out, even if
    # waiting for SSM, sending the command, or anything else below raises.
    instance_id = None
    try:
        log.info(
            f'Creating instance with image_id={run_args.image_id} '
            f'instance_type={run_args.instance_type} '
            f'subnet_id={run_args.subnet_id} '
            f'security_group_id={run_args.security_group_id} '
            f'instance_profile_arn={run_args.instance_profile_arn} '
            f'block_device_name={run_args.block_device_name} '
            f'block_volume_size_gb={run_args.block_volume_size_gb}'
        )
        instance_id = await create_instance(
            ec2,
            instance_name,
            image_id=run_args.image_id,
            instance_type=run_args.instance_type,
            subnet_id=run_args.subnet_id,
            security_group_ids=[run_args.security_group_id],
            instance_profile_arn=run_args.instance_profile_arn,
            block_device_name=run_args.block_device_name,
            block_volume_size_gb=run_args.block_volume_size_gb,
        )
        log.info(f'Created instance: {instance_id}')
        _cleanup_state.append((ec2, instance_id))

        log.info('Waiting for instance to become ready')
        await wait_for_instance(ec2, instance_id)

        ssm = session.client('ssm', region_name=run_args.region)

        log.info('Waiting for SSM')
        await wait_for_ssm(ssm, instance_id)

        if run_args.git_branch is not None:
            log.info(f'Checking out branch {run_args.git_branch}')
            await send_ssm_command(
                ssm,
                instance_id,
                make_git_checkout_command(run_args.git_branch),
            )

        log.info('Sending command')
        output = await send_ssm_command(
            ssm,
            instance_id,
            timeout_seconds=run_args.run_timeout_minutes * 60,
            command=make_command(
                sbmdt_instance_id,
                patch_type,
                pred_s3_key,
                apply_test_patch=run_args.apply_test_patch,
            ),
        )
        log.info(f'Received output: {output}')
    except Exception as e:
        # Re-raise so gather() records it and _report_results can surface
        # it. Logging alone made every failure invisible to the batch
        # summary, which then counted the instance as a success.
        log.error(
            f'Error running instance {sbmdt_instance_id} {patch_type}: {e}'
        )
        raise
    finally:
        if instance_id is not None:
            log.info('Terminating instance')
            await terminate_instance(ec2, instance_id)

    log.info('Done')


async def run_instance_async(
    sbmdt_instance_id: str,
    patch_type: PatchType,
    pred_s3_key: str,
    run_args: RunArgs,
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
        await run_instance(
            sbmdt_instance_id, patch_type, pred_s3_key, run_args
        )


def _request_shutdown(signum: int) -> None:
    """Sets the shutdown signal."""
    log.warning(f'Received signal {signum}, shutting down gracefully')
    _shutdown.set()
    log.warning('Shutdown signal has been set')


def _register_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Registers ``_request_shutdown`` for SIGINT/SIGTERM.

    ``loop.add_signal_handler`` is POSIX-only (``ProactorEventLoop`` on
    Windows raises ``NotImplementedError``), so this falls back to
    ``signal.signal`` there. The plain handler still runs on the main
    thread, same as the event loop, so calling ``_shutdown.set()`` from it
    directly is safe.
    """
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig)
        except NotImplementedError:
            signal.signal(sig, lambda signum, frame: _request_shutdown(signum))


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
        coros.append(terminate_instance(ec2, instance_id))
    await asyncio.gather(*coros, return_exceptions=True)


def _report_results(
    pred_s3_keys: list[str], work_tasks: asyncio.Future[list[Any]]
) -> None:
    """Log how the batch went and exit non-zero if anything failed.

    ``gather`` is called with ``return_exceptions=True`` so one bad
    instance cannot cancel the rest, but that also means a failure is
    only ever a value in the results list. Nothing used to read it, so a
    batch where every instance failed still logged ``Done!`` and exited
    0, which is indistinguishable from a batch that worked.

    Args:
        pred_s3_keys: The keys evaluated, in the order tasks were built.
        work_tasks: The gathered tasks, which may not have completed if
            the run was cut short by a shutdown request.

    Raises:
        SystemExit: If any instance raised.
    """
    if not work_tasks.done():
        log.warning('Work did not finish; results are incomplete')
        return

    results = work_tasks.result()
    failures = [
        (key, outcome)
        for key, outcome in zip(pred_s3_keys, results, strict=True)
        if isinstance(outcome, BaseException)
    ]

    succeeded = len(results) - len(failures)
    log.info(f'{succeeded}/{len(results)} instance(s) succeeded')

    if not failures:
        log.info('Done!')
        return

    log.error(f'{len(failures)} instance(s) failed:')
    for key, exc in failures:
        log.error(f'  {key}: {type(exc).__name__}: {exc}')
    raise SystemExit(1)


async def main(run_args: RunArgs) -> None:
    """Evaluate predictions in ``PREDS_S3_BUCKET_NAME``.

    Launches one EC2 instance per prediction file (up to ``N_CONCURRENT``
    at a time), shuffled so that a single unlucky batch of slow/large
    instances isn't processed all at once.

    Args:
        pred_keys: If given, only evaluate these S3 keys within
            ``PREDS_S3_BUCKET_NAME`` instead of every key in the bucket.
            Every key must exist in the bucket; if any don't, the run
            aborts before launching any instances.

    Registers the SIGINT/SIGTERM handler on this coroutine's running loop
    (``asyncio.run`` creates a fresh loop per call, so this can't be done
    beforehand). If a shutdown is requested before all instances finish,
    terminates every currently-tracked instance and waits for the
    already-running work to unwind before returning.
    """
    loop = asyncio.get_running_loop()
    _register_signal_handlers(loop)

    pred_keys = run_args.pred_keys
    all_pred_s3_keys = get_all_keys_in_s3_bucket(PREDS_S3_BUCKET_NAME)
    if pred_keys is None:
        pred_s3_keys = all_pred_s3_keys
    else:
        missing_keys = sorted(set(pred_keys) - set(all_pred_s3_keys))
        if missing_keys:
            raise ValueError(
                f'Requested pred key(s) not found in bucket '
                f'{PREDS_S3_BUCKET_NAME!r}: {missing_keys}'
            )
        pred_s3_keys = pred_keys

    tasks: list[Coroutine[Any, Any, None]] = []
    sem = asyncio.Semaphore(run_args.n_concurrent)
    for key in pred_s3_keys:
        pred_filename = S3PredFilename.decode(key)
        sbmdt_instance_id = pred_filename.instance_id
        patch_type = pred_filename.patch_type
        tasks.append(
            run_instance_async(
                sbmdt_instance_id, patch_type, key, run_args, sem
            )
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

    _report_results(pred_s3_keys, work_tasks)


def parse_args() -> RunArgs:
    """Parse CLI overrides for the AWS resource settings and other globals
    used throughout this module.

    Defaults are the module-level constants defined above.
    """
    parser = argparse.ArgumentParser(
        description=(
            'Evaluate predictions in PREDS_S3_BUCKET_NAME on a batch of '
            'short-lived EC2 instances. By default, evaluates every '
            'prediction in the bucket; use --pred-keys to evaluate a '
            'specific subset instead.'
        )
    )
    parser.add_argument(
        '--n-concurrent',
        type=int,
        default=N_CONCURRENT,
        help='Maximum number of EC2 instances running at the same time.',
    )
    parser.add_argument(
        '--pred-keys',
        nargs='+',
        default=None,
        metavar='KEY',
        help=(
            'Specific prediction file S3 key(s) to evaluate, instead of '
            'every key in PREDS_S3_BUCKET_NAME. All requested keys must '
            'already exist in the bucket, or the run aborts before '
            'launching any instances.'
        ),
    )
    parser.add_argument(
        '--image-id',
        default=IMAGE_ID,
        help='AMI ID to launch instances from.',
    )
    parser.add_argument(
        '--instance-type',
        default=INSTANCE_TYPE,
        choices=get_args(InstanceTypeType),
        # Avoids dumping the full 700+ item list in --help
        metavar='INSTANCE_TYPE',
        help='EC2 instance type to launch.',
    )
    parser.add_argument(
        '--subnet-id',
        default=SUBNET_ID,
        help='Subnet ID to launch instances into.',
    )
    parser.add_argument(
        '--security-group-id',
        default=SECURITY_GROUP_ID,
        help='Security group ID to attach to instances.',
    )
    parser.add_argument(
        '--instance-profile-arn',
        default=INSTANCE_PROFILE_ARN,
        help='IAM instance profile ARN to attach to instances.',
    )
    parser.add_argument(
        '--region',
        default=REGION,
        help='AWS region to launch instances in.',
    )
    parser.add_argument(
        '--block-device-name',
        default=BLOCK_DEVICE_NAME,
        help='Root block device name for the instance volume.',
    )
    parser.add_argument(
        '--block-volume-size-gb',
        type=int,
        default=BLOCK_VOLUME_SIZE_GB,
        help='Root block device volume size, in GB.',
    )
    parser.add_argument(
        '--aws-profile',
        default=AWS_PROFILE,
        help=(
            'Local AWS CLI profile used to create the boto3 session '
            '(rather than the default credential chain).'
        ),
    )
    parser.add_argument(
        '--apply-test-patch',
        action='store_true',
        help=(
            "Apply each instance's test patch on top of the model "
            "patch, so the maintainer's FAIL_TO_PASS tests are "
            'present regardless of what the model wrote. Without '
            'this every FAIL_TO_PASS test is reported as not run.'
        ),
    )
    parser.add_argument(
        '--run-timeout-minutes',
        type=int,
        default=DEFAULT_TIMEOUT_MINUTES,
        help=(
            'Kill an evaluation that has not finished within this '
            'many minutes. The default is generous; a successful '
            'run has a median of about 8 minutes, so a much lower '
            'value fails a hung instance fast instead of paying '
            'for it to sit until the ceiling.'
        ),
    )
    parser.add_argument(
        '--git-branch',
        default=GIT_BRANCH,
        help=(
            'If given, fetch and check out this git branch in /opt/sbmdt '
            'on each instance (via SSM) before running the evaluation '
            'command.'
        ),
    )

    args = parser.parse_args()

    return RunArgs(
        pred_keys=args.pred_keys,
        n_concurrent=args.n_concurrent,
        image_id=args.image_id,
        instance_type=args.instance_type,
        subnet_id=args.subnet_id,
        security_group_id=args.security_group_id,
        instance_profile_arn=args.instance_profile_arn,
        region=args.region,
        block_device_name=args.block_device_name,
        block_volume_size_gb=args.block_volume_size_gb,
        aws_profile=args.aws_profile,
        git_branch=args.git_branch,
        apply_test_patch=args.apply_test_patch,
        run_timeout_minutes=args.run_timeout_minutes,
    )


if __name__ == '__main__':
    setup_logging(level=logging.INFO)
    setup_logging_for_asyncio(log)
    run_args = parse_args()
    asyncio.run(main(run_args))
