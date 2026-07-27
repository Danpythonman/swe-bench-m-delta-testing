"""Async helpers for creating, waiting on, and terminating EC2 instances."""

from __future__ import annotations

import asyncio
import logging

from botocore.exceptions import ClientError, WaiterError
from mypy_boto3_ec2 import EC2Client
from mypy_boto3_ec2.literals import InstanceTypeType

__all__ = ['create_instance', 'wait_for_instance', 'terminate_instance']

log = logging.getLogger(__name__)

AUTO_SHUTDOWN_SCRIPT = """#!/bin/bash
shutdown -h +30
"""


async def create_instance(
    ec2: EC2Client,
    instance_name: str,
    image_id: str,
    instance_type: InstanceTypeType,
    subnet_id: str,
    security_group_ids: list[str],
    instance_profile_arn: str,
    block_device_name: str,
    block_volume_size_gb: int,
) -> str:
    """Launch a single EC2 instance and return its instance ID.

    Args:
        ec2: EC2 client used to issue the ``run_instances`` request.
        instance_name: Value to set as the instance's ``Name`` tag.
        image_id: AMI ID to launch the instance from.
        instance_type: EC2 instance type (e.g. ``t3.medium``).
        subnet_id: Subnet ID the instance is launched into.
        security_group_ids: Security group IDs to attach to the instance.
        instance_profile_arn: ARN of the IAM instance profile to attach.
        block_device_name: Device name for the root/data EBS volume
            (e.g. ``/dev/sda1``).
        block_volume_size_gb: Size in GiB of the ``gp3`` EBS volume.

    Returns:
        The ID of the newly created instance.

    Raises:
        Exception: If the response does not contain an instance ID.
    """
    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(
        None,
        lambda: ec2.run_instances(
            ImageId=image_id,
            InstanceType=instance_type,
            MinCount=1,
            MaxCount=1,
            SubnetId=subnet_id,
            SecurityGroupIds=security_group_ids,
            IamInstanceProfile={'Arn': instance_profile_arn},
            # Terminate on shutdown
            InstanceInitiatedShutdownBehavior='terminate',
            # Shutdown after 30 minutes automatically as a protection measure
            # in case the terminate script fails to terminate the instance
            UserData=AUTO_SHUTDOWN_SCRIPT,
            # Make a custom hard drive size
            BlockDeviceMappings=[
                {
                    'DeviceName': block_device_name,
                    'Ebs': {
                        'VolumeSize': block_volume_size_gb,
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
        ),
    )

    # MinCount/MaxCount above are both 1, so exactly one instance is
    # expected in the response.
    instance_id = response.get('Instances', [{}])[0].get('InstanceId', None)
    if instance_id is None:
        raise Exception('no instance id')

    return instance_id


async def wait_for_instance(ec2: EC2Client, instance_id: str) -> None:
    """Block until the given instance reaches the "running" state.

    Args:
        ec2: EC2 client used to poll instance state.
        instance_id: ID of the instance to wait for.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: ec2.get_waiter('instance_running').wait(
            InstanceIds=[instance_id]
        ),
    )


async def terminate_instance(
    ec2: EC2Client, instance_id: str, max_attempts: int = 5
) -> None:
    """Terminate the given instance and block until it is fully terminated.

    Retries the terminate call itself on transient AWS errors.

    Args:
        ec2: EC2 client used to issue the terminate request and wait.
        instance_id: ID of the instance to terminate.
        max_attempts: Maximum number of terminate attempts before giving up,
            with exponential backoff between retries.

    Raises:
        ClientError: If all terminate attempts fail.
    """
    loop = asyncio.get_running_loop()
    for attempt in range(1, max_attempts + 1):
        try:
            log.info(f'Attempting to terminate EC2 instance {instance_id}')
            await loop.run_in_executor(
                None,
                lambda: ec2.terminate_instances(InstanceIds=[instance_id]),
            )
            break
        except ClientError as e:
            log.error(f'Terminate attempt {attempt} failed: {e}')
            if attempt == max_attempts:
                raise
            await asyncio.sleep(2**attempt)

    try:
        log.info(f'Waiting for EC2 instance {instance_id} to terminate')
        await loop.run_in_executor(
            None,
            lambda: ec2.get_waiter('instance_terminated').wait(
                InstanceIds=[instance_id]
            ),
        )
        log.info(f'{instance_id} terminated')
    except WaiterError as e:
        # Terminate request was accepted; waiter failing doesn't mean the
        # instance is still running. Log and don't mask it as total failure.
        log.error(f'Waiter for termination failed, verify manually: {e}')
