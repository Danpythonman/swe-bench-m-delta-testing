"""Conversion of evaluation results to and from the Parquet format."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Final

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from sbmdt.aws.s3 import buffer_to_s3
from sbmdt.evaluator.base import TestResult

__all__ = [
    'TEST_RESULTS_SCHEMA',
    'read_test_results',
    'test_results_to_parquet_table',
    'parquet_table_to_file',
    'parquet_table_to_s3',
]


TEST_RESULTS_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        ('instance_id', pa.string()),
        ('patch_type', pa.string()),
        ('agent_name', pa.string()),
        ('timestamp', pa.timestamp('us', tz='UTC')),
        ('test_name', pa.string()),
        ('passed', pa.bool_()),
        ('test_file', pa.string()),
    ]
)


def read_test_results(path: Path) -> pd.DataFrame:
    """Read a directory of result objects under one schema.

    Results written before ``test_file`` existed lack that column.
    Reading the directory with pandas alone takes the schema from
    whichever file it sees first and silently drops the column from every
    other file, so the full schema is imposed here and older files read
    it as null.

    Args:
        path: Directory of Parquet result objects (or a single file).

    Returns:
        One row per test result.
    """
    dataset = ds.dataset(path, format='parquet', schema=TEST_RESULTS_SCHEMA)
    return dataset.to_table().to_pandas()


def test_results_to_parquet_table(results: list[TestResult]) -> pa.Table:
    """Convert a list of :class:`TestResult` into an Arrow table.

    Args:
        results: The test results to convert.

    Returns:
        A :class:`pyarrow.Table` with one row per result and columns
        ``instance_id``, ``patch_type``, ``agent_name``, ``timestamp``,
        ``test_name``, ``passed`` and ``test_file`` (null when unknown).
    """

    table = pa.Table.from_pylist(
        [r.to_dict() for r in results], schema=TEST_RESULTS_SCHEMA
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
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    buffer.seek(0)
    buffer_to_s3(buffer, bucket, key, overwrite)
