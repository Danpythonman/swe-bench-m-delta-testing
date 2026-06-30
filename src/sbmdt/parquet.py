"""Conversion of evaluation results to and from the Parquet format."""

from __future__ import annotations

import io
from pathlib import Path

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from sbmdt.evaluator.base import TestResult

__all__ = [
    'test_results_to_parquet_table',
    'parquet_table_to_file',
    's3_object_exists',
    'parquet_table_to_s3',
]


def test_results_to_parquet_table(results: list[TestResult]) -> pa.Table:
    """Convert a list of :class:`TestResult` into an Arrow table.

    Args:
        results: The test results to convert.

    Returns:
        A :class:`pyarrow.Table` with one row per result and columns
        ``instance_id``, ``patch_type``, ``agent_name``, ``test_name``,
        and ``passed``.
    """

    table = pa.Table.from_pylist(
        [r.to_dict() for r in results],
        schema=pa.schema(
            [
                ('instance_id', pa.string()),
                ('patch_type', pa.string()),
                ('agent_name', pa.string()),
                ('timestamp', pa.timestamp('us', tz='UTC')),
                ('test_name', pa.string()),
                ('passed', pa.bool_()),
            ]
        ),
    )

    return table


def parquet_table_to_file(
    table: pa.Table, filepath: Path, overwrite: bool = False
) -> None:
    """Write an Arrow table to a local Parquet file.

    Args:
        table: The table to write.
        filepath: Destination path for the Parquet file.
        overwrite: If False (default), raises FileExistsError if the file
                   already exists.
    """
    if not overwrite and filepath.exists():
        raise FileExistsError(f'File already exists: {filepath}')
    pq.write_table(table, filepath)


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


def parquet_table_to_s3(
    table: pa.Table, bucket: str, key: str, overwrite: bool = False
) -> None:
    """Write an Arrow table to S3 as a Parquet object.

    Serializes ``table`` to Parquet in memory and uploads it without
    writing to local disk.

    Args:
        table: The table to write.
        bucket: Destination S3 bucket name.
        key: Destination object key within ``bucket``.
        overwrite: If False (default), raises FileExistsError if the object
                   already exists.
    """
    if not overwrite and s3_object_exists(bucket, key):
        raise FileExistsError(f'S3 object already exists: s3://{bucket}/{key}')

    client = boto3.client('s3')
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    buffer.seek(0)
    client.put_object(Body=buffer, Bucket=bucket, Key=key)
