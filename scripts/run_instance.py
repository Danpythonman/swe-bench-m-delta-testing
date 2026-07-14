import argparse
import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sbmdt import evaluate
from sbmdt.env import PROJECT_BASE
from sbmdt.evaluator.base import PatchType, TestResultsFilename
from sbmdt.log import setup_logging
from sbmdt.parquet import (
    parquet_table_to_file,
    parquet_table_to_s3,
    test_results_to_parquet_table,
)
from sbmdt.pred import Pred
from sbmdt.s3 import (
    TEST_RESULTS_S3_BUCKET_NAME,
    load_pred_from_s3,
    s3_object_exists,
)
from sbmdt.timing import log_duration

log = logging.getLogger(__name__)


@dataclass(kw_only=True)
class Args:
    instance_id: str
    log_file: Path
    patch_type: PatchType
    pred: Pred | None
    output_format: Literal['json'] | Literal['parquet']
    destination: Literal['file'] | Literal['s3']
    bucket: str
    overwrite: bool


def parse_args() -> Args:
    parser = argparse.ArgumentParser(
        description='Run delta testing evaluation for a benchmark instance.'
    )

    parser.add_argument(
        'instance_id',
        help=(
            'Benchmark instance ID to evaluate, e.g. '
            "'alibaba-fusion__next-717'."
        ),
    )
    parser.add_argument(
        'patch_type',
        type=PatchType,
        choices=list(PatchType),
        help='Patch state under which the test was executed.',
    )
    parser.add_argument(
        '--pred-file',
        type=str,
        default=None,
        help=(
            'Path to .pred file, or an s3:// URL '
            '(required unless patch_type is before_patch).'
        ),
    )
    parser.add_argument(
        '--log-file',
        type=Path,
        default=PROJECT_BASE / 'logs' / 'log.log',
        help='Path to write logs to (default: logs/log.log).',
    )

    format_group = parser.add_mutually_exclusive_group(required=True)
    format_group.add_argument(
        '--json',
        action='store_const',
        dest='output_format',
        const='json',
        help='Write results as JSON.',
    )
    format_group.add_argument(
        '--parquet',
        action='store_const',
        dest='output_format',
        const='parquet',
        help='Write results as Parquet.',
    )

    dest_group = parser.add_mutually_exclusive_group(required=True)
    dest_group.add_argument(
        '--s3',
        action='store_const',
        dest='destination',
        const='s3',
        help='Upload Parquet files to S3.',
    )
    dest_group.add_argument(
        '--file',
        action='store_const',
        dest='destination',
        const='file',
        help='Write files to disk alongside the source JSON files.',
    )

    parser.add_argument(
        '--bucket',
        type=str,
        default=TEST_RESULTS_S3_BUCKET_NAME,
        help='S3 bucket name. Required when --s3 is set.',
    )
    parser.add_argument(
        '--overwrite',
        action='store_true',
        help='Overwrite when results already exist.',
    )

    ns = parser.parse_args()

    if ns.patch_type == PatchType.BEFORE_PATCH and ns.pred_file is not None:
        parser.error(
            '--pred-file should not be provided when patch_type is '
            'before_patch.'
        )
    if ns.patch_type != PatchType.BEFORE_PATCH and ns.pred_file is None:
        parser.error(
            '--pred-file is required when patch_type is not before_patch.'
        )
    if ns.output_format == 'json' and ns.destination != 'file':
        parser.error('--json output is only supported with --file.')
    if ns.destination == 's3' and ns.output_format != 'parquet':
        parser.error('--s3 destination is only supported with --parquet.')

    pred: Pred | None = None
    if ns.pred_file is not None:
        if str(ns.pred_file).startswith('s3://'):
            pred = Pred.from_file_contents(load_pred_from_s3(ns.pred_file))
        else:
            pred_path = Path(ns.pred_file)
            if not pred_path.exists():
                parser.error(f'--pred-file does not exist: {pred_path}')
            pred = Pred.from_file(pred_path)

    return Args(
        instance_id=ns.instance_id,
        log_file=ns.log_file,
        patch_type=ns.patch_type,
        pred=pred,
        output_format=ns.output_format,
        destination=ns.destination,
        bucket=ns.bucket,
        overwrite=ns.overwrite,
    )


@log_duration(logger=log)
def run_instance(args: Args):
    results_dir = PROJECT_BASE / 'results'
    results_dir.mkdir(exist_ok=True)

    timestamp = dt.datetime.now(dt.UTC)
    test_result_filename = TestResultsFilename(
        instance_id=args.instance_id,
        patch_type=args.patch_type,
        agent_name=Pred.get_agent_name(args.pred),
        timestamp=timestamp,
    )
    results_path = results_dir / test_result_filename.encode()

    if args.output_format == 'json':
        results_path = results_path.with_suffix('.json')
    else:
        results_path = results_path.with_suffix('.parquet')

    if args.destination == 'file':
        if results_path.exists() and not args.overwrite:
            log.warning(
                'Skipping because results already exist at '
                f'`{results_path.resolve()}` (use --overwrite to replace).'
            )
            return
    else:
        if (
            s3_object_exists(args.bucket, test_result_filename.encode())
            and not args.overwrite
        ):
            log.warning(
                'Skipping because results already exist in S3 bucket='
                f'{args.bucket} and key={results_path.name} (use --overwrite '
                'to replace).'
            )
            return

    log.info('Starting evaluation')

    results = evaluate(
        instance_id=args.instance_id,
        timestamp=timestamp,
        patch_type=args.patch_type,
        pred=args.pred,
    )

    log.info('Evaluation complete, saving results')

    if args.output_format == 'json':
        log.info('Dumping results as local JSON file')
        with open(results_path, 'w') as f:
            json.dump(
                [r.to_dict(json_safe=True) for r in results], f, indent=4
            )
    else:
        parquet_table = test_results_to_parquet_table(results)
        if args.destination == 'file':
            log.info('Dumping results as local parquet file')
            parquet_table_to_file(
                parquet_table,
                results_path,
                args.overwrite,
            )
        else:
            log.info('Dumping results as S3 parquet file')
            parquet_table_to_s3(
                parquet_table,
                args.bucket,
                test_result_filename.encode(),
                args.overwrite,
            )

    log.info('Saving results complete')

def main() -> None:
    args = parse_args()

    log_path = Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    setup_logging(log_file=log_path, level=logging.INFO)

    run_instance(args)


if __name__ == '__main__':
    main()
