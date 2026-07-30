"""Terminate a single EC2 instance given its instance ID.

Counterpart to ``start_single_ec2.py``: use this to tear down an instance
started there (or any other ``sbmdt`` instance) once you're done debugging.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass

import boto3

from sbmdt.aws.ec2 import terminate_instance
from sbmdt.aws.env import AWS_PROFILE, REGION
from sbmdt.log import setup_logging, setup_logging_for_asyncio

log = logging.getLogger(__name__)


@dataclass(kw_only=True)
class RunArgs:
    instance_id: str
    region: str
    aws_profile: str


def parse_args() -> RunArgs:
    """Parse CLI arguments: the instance to terminate, plus AWS overrides.

    Defaults for the AWS overrides are the module-level constants defined
    above.
    """
    parser = argparse.ArgumentParser(
        description=(
            'Terminate a single EC2 instance and wait for it to reach the '
            'terminated state.'
        )
    )
    parser.add_argument(
        'instance_id',
        help='ID of the EC2 instance to terminate.',
    )
    parser.add_argument(
        '--region',
        default=REGION,
        help='AWS region the instance is running in.',
    )
    parser.add_argument(
        '--aws-profile',
        default=AWS_PROFILE,
        help=(
            'Local AWS CLI profile used to create the boto3 session '
            '(rather than the default credential chain).'
        ),
    )

    args = parser.parse_args()

    return RunArgs(
        instance_id=args.instance_id,
        region=args.region,
        aws_profile=args.aws_profile,
    )


async def main(run_args: RunArgs) -> None:
    log.info('Starting session')
    session = boto3.Session(profile_name=run_args.aws_profile)
    ec2 = session.client('ec2', region_name=run_args.region)

    log.info(f'Terminating instance {run_args.instance_id}')
    await terminate_instance(ec2, run_args.instance_id)


if __name__ == '__main__':
    setup_logging(level=logging.INFO)
    setup_logging_for_asyncio(log)
    run_args = parse_args()
    asyncio.run(main(run_args))
