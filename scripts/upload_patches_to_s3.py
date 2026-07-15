from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from dotenv import load_dotenv

from sbmdt.env import DOCKERFILES_BASE
from sbmdt.evaluator.base import PatchType
from sbmdt.log import setup_logging
from sbmdt.pred import Pred
from sbmdt.s3 import PREDS_S3_BUCKET_NAME, S3PredFilename, file_to_s3

log = logging.getLogger(__name__)


def upload_single_pred_to_s3(pred_filepath: Path, patch_type: PatchType, ignore_patch: bool = False):
    """Upload a single .pred file to S3 with a standardized filename.

    The new filename is built from the instance id (the source filename),
    the patch type, the agent name recorded in the pred file, and the
    file's last-modified timestamp (UTC).

    Args:
        pred_filepath: Path to the local .pred file to upload.
        patch_type: Label identifying the kind of patch (e.g. 'with-images',
            'without-images', 'gold'), used in the uploaded filename.
    """
    pred_filestat = pred_filepath.stat()
    pred_file_timestamp = pred_filestat.st_mtime
    pred_file_timestamp_dt = dt.datetime.fromtimestamp(
        pred_file_timestamp, tz=dt.UTC
    )

    pred = Pred.from_file(pred_filepath)
    instance_id = pred.instance_id

    if ignore_patch:
        pred.model_name_or_path = 'NONE'
        pred.model_patch = ''

    s3_pred_filename = S3PredFilename(
        instance_id=instance_id,
        patch_type=patch_type,
        agent_name=Pred.get_agent_name(pred),
        timestamp=pred_file_timestamp_dt,
    )

    log.info(
        f'Uploading {pred_filepath} to S3 bucket {PREDS_S3_BUCKET_NAME} with '
        f'key {s3_pred_filename.encode()}'
    )
    try:
        file_to_s3(
            pred_filepath, PREDS_S3_BUCKET_NAME, s3_pred_filename.encode()
        )
    except FileExistsError:
        log.warning(
            f'S3 bucket {PREDS_S3_BUCKET_NAME} already has an object with '
            f'key {s3_pred_filename.encode()}'
        )


def upload_preds_to_s3(
    preds_dir: Path, patch_type: PatchType, recursive: bool = True, ignore_patch: bool = False
):
    """Upload .pred files found under a directory to S3.

    Args:
        preds_dir: Root directory to search for .pred files.
        patch_type: Label identifying the kind of patch, passed through to
            each individual upload.
        recursive: If True, search subdirectories (rglob). If False, search
            only preds_dir itself (glob).
    """
    glob_fn = preds_dir.rglob if recursive else preds_dir.glob
    for pred_filepath in glob_fn('*.pred'):
        upload_single_pred_to_s3(pred_filepath, patch_type, ignore_patch=ignore_patch)


def main():
    """Upload the three known prediction sets (with-images, without-images,
    and gold) to S3.
    """
    setup_logging(level=logging.INFO)
    load_dotenv()

    with_images_dir = Path(
        '/home/daniel/York/Masters/EECS6444/Project/preds/with-images'
    )
    upload_preds_to_s3(with_images_dir, PatchType.WITH_IMAGE)

    without_images_dir = Path(
        '/home/daniel/York/Masters/EECS6444/Project/preds/without-images'
    )
    upload_preds_to_s3(without_images_dir, PatchType.WITHOUT_IMAGE)

    gold_patches_dir = DOCKERFILES_BASE
    upload_preds_to_s3(gold_patches_dir, PatchType.GOLD)
    upload_preds_to_s3(gold_patches_dir, PatchType.BEFORE_PATCH)


if __name__ == '__main__':
    main()
