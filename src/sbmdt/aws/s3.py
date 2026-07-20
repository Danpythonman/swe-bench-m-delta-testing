"""Utilities for reading and writing SWE-bench predictions and test results
to and from S3.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

import boto3

from sbmdt.evaluator.base import PatchType

__all__ = [
    's3_object_exists',
    'buffer_to_s3',
    'file_to_s3',
    'load_pred_from_s3',
    'get_all_keys_in_s3_bucket',
    'S3PredFilename',
    'PREDS_S3_BUCKET_NAME',
    'STDOUT_S3_BUCKET_NAME',
    'TEST_RESULTS_S3_BUCKET_NAME',
]

PREDS_S3_BUCKET_NAME: Final[str] = 'sbmdt-preds'

TEST_RESULTS_S3_BUCKET_NAME: Final[str] = 'sbmdt-test-results'

STDOUT_S3_BUCKET_NAME: Final[str] = 'sbmdt-stdout'

_TIMESTAMP_FMT: Final[str] = '%Y-%m-%d_%H-%M-%SZ'
_TIMESTAMP_RE: Final[str] = r'\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}Z'
_FILENAME_RE: Final[re.Pattern[str]] = re.compile(
    rf'^(?P<instance_id>.+)_(?P<patch_type>.+)_(?P<agent_name>.+)_'
    rf'(?P<timestamp>{_TIMESTAMP_RE})\.pred$'
)


@dataclass(kw_only=True)
class S3PredFilename:
    """A parsed/structured representation of a prediction filename in S3.

    Prediction files are stored in S3 with filenames of the form
    ``{instance_id}_{patch_type}_{agent_name}_{timestamp}.pred``, where each
    field is escaped to keep the ``_`` field separator unambiguous. This
    class provides ``encode``/``decode`` methods to convert between this
    dataclass and that filename format.

    Attributes:
        instance_id: The SWE-bench instance ID the prediction is for.
        patch_type: The type of patch the prediction represents.
        agent_name: The name of the agent that produced the prediction.
        timestamp: The UTC timestamp of when the prediction was created.
    """

    instance_id: str
    patch_type: PatchType
    agent_name: str
    timestamp: dt.datetime

    @staticmethod
    def _escape(s: str) -> str:
        """Escape a field value so it can be safely embedded in a filename.

        Args:
            s: The raw field value to escape.

        Returns:
            The escaped value, with '%' and '_' replaced with sequences that
            cannot collide with the '_' field separator.
        """
        # make '_' unambiguous inside a field
        return s.replace('%', '%25').replace('_', '%5F')

    @staticmethod
    def _unescape(s: str) -> str:
        """Reverse the escaping applied by ``_escape``.

        Args:
            s: The escaped field value, as extracted from a filename.

        Returns:
            The original, unescaped field value.
        """
        return s.replace('%5F', '_').replace('%25', '%')

    def encode(self, extension: str = '.pred') -> str:
        """Encode this instance as a prediction filename.

        Returns:
            The filename in the form
            ``{instance_id}_{patch_type}_{agent_name}_{timestamp}.pred``,
            with each field escaped and the timestamp converted to UTC.
        """
        ts = self.timestamp.astimezone(dt.UTC).strftime(_TIMESTAMP_FMT)
        return (
            f'{S3PredFilename._escape(self.instance_id)}_'
            f'{S3PredFilename._escape(self.patch_type)}_'
            f'{S3PredFilename._escape(self.agent_name)}_'
            f'{ts}{extension}'
        )

    @staticmethod
    def decode(filename: str) -> S3PredFilename:
        """Parse a prediction filename into an ``S3PredFilename`` instance.

        Args:
            filename: The filename to parse, as produced by ``encode``.

        Returns:
            The decoded ``S3PredFilename``, with the timestamp set to UTC.

        Raises:
            ValueError: If ``filename`` does not match the expected format.
        """
        m = _FILENAME_RE.match(filename)
        if not m:
            raise ValueError(f'Cannot parse filename: {filename!r}')
        ts = dt.datetime.strptime(m['timestamp'], _TIMESTAMP_FMT).replace(
            tzinfo=dt.UTC
        )
        return S3PredFilename(
            instance_id=S3PredFilename._unescape(m['instance_id']),
            patch_type=PatchType(S3PredFilename._unescape(m['patch_type'])),
            agent_name=S3PredFilename._unescape(m['agent_name']),
            timestamp=ts,
        )


def _raise_exception_if_s3_object_exists(
    bucket: str, key: str, overwrite: bool
) -> None:
    """Raises an exception if an S3 object already exists in a bucket and
    overwrite is not set to ``True``.
    """
    if not overwrite and s3_object_exists(bucket, key):
        raise FileExistsError(f'S3 object already exists: s3://{bucket}/{key}')


def s3_object_exists(bucket: str, key: str) -> bool:
    """Check whether an object exists in S3.

    Args:
        bucket: S3 bucket name.
        key: Object key within ``bucket``.

    Returns:
        True if the object exists, False otherwise.
    """
    client = boto3.client('s3')
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except client.exceptions.ClientError as e:
        if e.response.get('Error', {}).get('Code') != '404':
            raise
        return False


def buffer_to_s3(
    buffer: io.BytesIO, bucket: str, key: str, overwrite: bool = False
) -> None:
    """Write a buffer to an S3 bucket.

    Args:
        bucket: Destination S3 bucket name.
        key: Destination object key within ``bucket``.
        overwrite: If False (default), raises FileExistsError if the object
                   already exists.
    """
    _raise_exception_if_s3_object_exists(bucket, key, overwrite)
    client = boto3.client('s3')
    client.put_object(Body=buffer, Bucket=bucket, Key=key)


def file_to_s3(
    path: Path, bucket: str, key: str, overwrite: bool = False
) -> None:
    """Copy a file to S3.

    Args:
        path: The path of the file to copy.
        bucket: Destination S3 bucket name.
        key: Destination object key within ``bucket``.
        overwrite: If False (default), raises FileExistsError if the object
                   already exists.
    """
    _raise_exception_if_s3_object_exists(bucket, key, overwrite)
    client = boto3.client('s3')
    client.upload_file(str(path.resolve()), bucket, key)


def load_pred_from_s3(pred_file: str) -> bytes:
    """Download a prediction file's contents from S3.

    Args:
        pred_file: An ``s3://<bucket>/<key>`` URL pointing to the prediction
            file to download.

    Returns:
        The raw bytes of the object's contents.
    """
    parsed = urlparse(pred_file)
    bucket = parsed.netloc
    key = parsed.path.lstrip('/')
    client = boto3.client('s3')
    obj = client.get_object(Bucket=bucket, Key=key)
    return obj['Body'].read()


def get_all_keys_in_s3_bucket(bucket_name: str) -> list[str]:
    """List the keys of all objects in an S3 bucket.

    Args:
        bucket_name: The name of the S3 bucket to list.

    Returns:
        The keys of every object in the bucket, across all pages.
    """
    client = boto3.client('s3')
    paginator = client.get_paginator('list_objects_v2')
    filenames: list[str] = []

    for page in paginator.paginate(Bucket=bucket_name):
        for obj in page.get('Contents', []):
            key = obj.get('Key', None)
            if key is not None:
                filenames.append(key)

    return filenames
