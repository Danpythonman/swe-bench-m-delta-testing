import argparse
import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv

from sbmdt.env import PROJECT_BASE
from sbmdt.evaluator.base import TestResult
from sbmdt.log import setup_logging
from sbmdt.parquet import (
    parquet_table_to_file,
    parquet_table_to_s3,
    test_results_to_parquet_table,
)

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Convert test result JSON files to Parquet format.'
    )
    output_group = parser.add_mutually_exclusive_group(required=True)
    output_group.add_argument(
        '--s3',
        action='store_true',
        help='Upload Parquet files to S3.',
    )
    output_group.add_argument(
        '--file',
        action='store_true',
        help='Write Parquet files to disk alongside the source JSON files.',
    )
    parser.add_argument(
        '--bucket',
        type=str,
        default='sbmdt-test-results',
        help='S3 bucket name. Required when --s3 is set.',
    )
    parser.add_argument(
        '--overwrite',
        action='store_true',
        help='Overwrite when results already exist.',
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.s3:
        if not args.bucket:
            raise ValueError('--bucket is required when --s3 is set.')
        if not os.environ.get('AWS_ACCESS_KEY_ID', None):
            raise ValueError(
                'AWS_ACCESS_KEY_ID env var required when --s3 is set.'
            )
        if not os.environ.get('AWS_SECRET_ACCESS_KEY'):
            raise ValueError(
                'AWS_SECRET_ACCESS_KEY env var required when --s3 is set.'
            )


def process_results(args: argparse.Namespace) -> None:
    results_dir: Path = PROJECT_BASE / 'results'

    if not results_dir.exists():
        raise FileNotFoundError(f'Results directory not found: {results_dir}')

    for result_filepath in results_dir.glob('*.json'):
        log.info(f'Processing {result_filepath}')
        with open(result_filepath) as f:
            obj = json.load(f)

        if not isinstance(obj, list):
            log.warning(
                f'Skipping {result_filepath.name}: expected a JSON array.'
            )
            continue
        obj = cast(list[dict[str, Any]], obj)

        now = dt.datetime.now(dt.UTC)
        obj = [
            item
            if 'timestamp' in item
            else {**item, 'timestamp': now.isoformat()}
            for item in obj
        ]
        test_results = [TestResult.from_dict(item) for item in obj]
        parquet_table = test_results_to_parquet_table(test_results)

        if args.s3:
            parquet_table_to_s3(
                parquet_table,
                args.bucket,
                result_filepath.with_suffix('.parquet').name,
                args.overwrite,
            )
        else:
            parquet_table_to_file(
                parquet_table,
                result_filepath.with_suffix('.parquet'),
                args.overwrite,
            )


def main() -> None:
    setup_logging()
    load_dotenv()
    args = parse_args()
    validate_args(args)
    process_results(args)


if __name__ == '__main__':
    main()
