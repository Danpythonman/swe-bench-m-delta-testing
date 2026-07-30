"""Launch a single short-lived EC2 instance and leave it running so a user
can SSM into it to debug.

Used to spin up an ad-hoc instance built from ``IMAGE_ID`` for interactive
debugging, as opposed to ``run_all_ec2.py`` which launches a batch of
instances to evaluate predictions and terminates each one when it's done.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import get_args

import boto3
from mypy_boto3_ec2.literals import InstanceTypeType
from run_all_ec2 import make_git_checkout_command

from sbmdt.aws.ec2 import (
    create_instance,
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
from sbmdt.aws.ssm import send_ssm_command, wait_for_ssm
from sbmdt.log import setup_logging, setup_logging_for_asyncio

log = logging.getLogger(__name__)


@dataclass(kw_only=True)
class RunArgs:
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


def parse_args() -> RunArgs:
    """Parse CLI overrides for the AWS resource settings and other globals
    used throughout this module.

    Defaults are the module-level constants defined above.
    """
    parser = argparse.ArgumentParser(
        description=(
            'Launch a single short-lived EC2 instance from IMAGE_ID and '
            'wait for it to become reachable via SSM, for interactive '
            'debugging. Prints the instance ID on completion; the '
            'instance is left running for the caller to SSM into and '
            'terminate manually when done.'
        )
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
        '--git-branch',
        default=None,
        help=(
            'If given, fetch and check out this git branch in /opt/sbmdt '
            'on each instance (via SSM) before running the evaluation '
            'command.'
        ),
    )

    args = parser.parse_args()

    return RunArgs(
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
    )


async def start_instance(run_args: RunArgs) -> str:
    """Create an EC2 instance and wait for it to become reachable via SSM,
    optionally checking out ``run_args.git_branch`` on it.

    Returns the instance ID; the instance is left running for the caller
    to SSM into and terminate manually when done debugging.
    """

    now = dt.datetime.now(tz=dt.UTC)
    # Timestamp suffix keeps instance names unique across runs.
    instance_name = f'sbmdt-ec2-{now.timestamp()}'

    log.info('Starting session')
    session = boto3.Session(profile_name=run_args.aws_profile)
    ec2 = session.client('ec2', region_name=run_args.region)

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

    log.info('Waiting for instance to become ready')
    await wait_for_instance(ec2, instance_id)

    ssm = session.client('ssm', region_name=run_args.region)

    log.info('Waiting for SSM')
    await wait_for_ssm(ssm, instance_id)

    if run_args.git_branch is not None:
        log.info(f'Checking out branch {run_args.git_branch}')
        await send_ssm_command(
            ssm, instance_id, make_git_checkout_command(run_args.git_branch)
        )

    log.info(f'EC2 instance {instance_id} is ready')
    return instance_id


async def main(run_args: RunArgs):
    instance_id = await start_instance(run_args)
    print(f'\n\n\n{instance_id}\n\n\n')


if __name__ == '__main__':
    setup_logging(level=logging.INFO)
    setup_logging_for_asyncio(log)
    run_args = parse_args()
    asyncio.run(main(run_args))
