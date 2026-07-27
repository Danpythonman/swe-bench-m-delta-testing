from __future__ import annotations

import random
from pathlib import Path
from typing import Final

from sbmdt.aws.s3 import (
    PREDS_S3_BUCKET_NAME,
    TEST_RESULTS_S3_BUCKET_NAME,
    S3PredFilename,
    get_all_keys_in_s3_bucket,
)
from sbmdt.env import PROJECT_BASE
from sbmdt.evaluator.base import PatchType

PYTHON_COMMAND: Final[str] = 'uv run'
SCRIPT_PATH: Final[Path] = PROJECT_BASE / 'scripts' / 'run_instance.py'


def make_command(
    instance_id: str,
    patch_type: PatchType,
    pred_key: str,
    pred_bucket_name: str = PREDS_S3_BUCKET_NAME,
    test_results_s3_bucket_name: str = TEST_RESULTS_S3_BUCKET_NAME,
) -> str:
    return ' '.join(
        [
            PYTHON_COMMAND,
            str(SCRIPT_PATH.resolve()),
            instance_id,
            patch_type,
            f'--pred-file s3://{pred_bucket_name}/{pred_key}',
            '--parquet',
            '--s3',
            f'--bucket {test_results_s3_bucket_name}',
        ]
    )


def main():
    pred_keys = get_all_keys_in_s3_bucket(PREDS_S3_BUCKET_NAME)
    s3_pred_filenames = [
        S3PredFilename.decode(pred_key) for pred_key in pred_keys
    ]
    commands = [
        make_command(
            instance_id=s3_pred_filename.instance_id,
            patch_type=s3_pred_filename.patch_type,
            pred_key=s3_pred_filename.encode(),
            pred_bucket_name=PREDS_S3_BUCKET_NAME,
            test_results_s3_bucket_name=TEST_RESULTS_S3_BUCKET_NAME,
        )
        for s3_pred_filename in s3_pred_filenames
    ]

    random.shuffle(commands)

    with open(PROJECT_BASE / 'tasks.txt', 'w') as f:
        f.write('\n'.join(commands))


if __name__ == '__main__':
    main()
