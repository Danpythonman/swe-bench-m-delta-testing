"""Defaults for AWS environment variables."""

from typing import Final

from mypy_boto3_ec2.literals import InstanceTypeType

IMAGE_ID: Final[str] = 'ami-070ff54484e26f9bb'
"""Default AMI ID used by ``scripts/run_all_ec2.py`` to launch instances."""

INSTANCE_TYPE: Final[InstanceTypeType] = 't3a.large'
"""Default EC2 instance type used by ``scripts/run_all_ec2.py``."""

SUBNET_ID: Final[str] = 'subnet-0d1aeaaad9a22c741'
"""Default subnet ID used by ``scripts/run_all_ec2.py``."""

SECURITY_GROUP_ID: Final[str] = 'sg-09f9a76d742f8549d'
"""Default security group ID used by ``scripts/run_all_ec2.py``."""

INSTANCE_PROFILE_ARN: Final[str] = (
    'arn:aws:iam::607869540801:instance-profile/sbmdt-instance-profile'
)
"""Default IAM instance profile ARN used by ``scripts/run_all_ec2.py``."""

REGION: Final[str] = 'us-east-1'
"""Default AWS region used by ``scripts/run_all_ec2.py``."""

BLOCK_DEVICE_NAME: Final[str] = '/dev/xvda'
"""Default root block device name used by ``scripts/run_all_ec2.py``."""

BLOCK_VOLUME_SIZE_GB: Final[int] = 16
"""Default root block device volume size, in GB, used by
``scripts/run_all_ec2.py``.
"""

AWS_PROFILE: Final[str] = 'admin-user'
"""Default local AWS CLI profile used by ``scripts/run_all_ec2.py`` to
create its boto3 session, rather than the default credential chain.
"""

GIT_BRANCH: Final[str | None] = None
"""Default git branch checked out on the instance by
``scripts/run_all_ec2.py``; ``None`` means no checkout is performed.
"""

N_CONCURRENT: Final[int] = 5
"""Default maximum number of EC2 instances ``scripts/run_all_ec2.py`` allows
to be running (i.e. mid-evaluation) at the same time.
"""

__all__ = [
    'AWS_PROFILE',
    'BLOCK_DEVICE_NAME',
    'BLOCK_VOLUME_SIZE_GB',
    'GIT_BRANCH',
    'IMAGE_ID',
    'INSTANCE_PROFILE_ARN',
    'INSTANCE_TYPE',
    'N_CONCURRENT',
    'REGION',
    'SECURITY_GROUP_ID',
    'SUBNET_ID',
]
