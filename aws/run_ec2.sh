#!/usr/bin/env bash

# Runs a single delta-testing evaluation and uploads the result/logs to S3.
#
# This script is not meant to be invoked directly; it is built into the AMI
# referenced by IMAGE_ID in scripts/run_ec2.py and executed remotely via SSM
# (see `make_command` there), from the /opt/sbmdt working directory.
#
# Flags (all supplied by scripts/run_ec2.py's make_command):
#   --instance-id      sbmdt_instance_id     - benchmark instance ID to
#                                               evaluate.
#   --patch-type       patch_type            - patch state to evaluate
#                                               under (PatchType value, e.g.
#                                               "with_image").
#   --pred-bucket      s3_pred_bucket_name   - S3 bucket containing the
#                                               input .pred file.
#   --pred-key         s3_pred_key           - S3 key of the input .pred
#                                               file.
#   --results-bucket   s3_test_results_bucket_name - S3 bucket to upload
#                                               the test results to.
#   --stdout-bucket    s3_stdout_bucket_name - S3 bucket to upload this
#                                               run's log to.
#   --stdout-key       s3_stdout_key         - S3 key to upload this run's
#                                               log to.

set -euo pipefail

INSTANCE_ID=
PATCH_TYPE=
PRED_BUCKET=
PRED_KEY=
RESULTS_BUCKET=
STDOUT_BUCKET=
STDOUT_KEY=

while [[ $# -gt 0 ]]; do
    case "$1" in
        --instance-id)
            INSTANCE_ID="$2"
            shift 2
            ;;
        --patch-type)
            PATCH_TYPE="$2"
            shift 2
            ;;
        --pred-bucket)
            PRED_BUCKET="$2"
            shift 2
            ;;
        --pred-key)
            PRED_KEY="$2"
            shift 2
            ;;
        --results-bucket)
            RESULTS_BUCKET="$2"
            shift 2
            ;;
        --stdout-bucket)
            STDOUT_BUCKET="$2"
            shift 2
            ;;
        --stdout-key)
            STDOUT_KEY="$2"
            shift 2
            ;;
        *)
            echo "run_ec2.sh: unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

# Fail loudly if the caller forgot a required flag, rather than silently
# proceeding with an empty value (e.g. an empty --pred-bucket would turn
# into `s3:///<key>`).
for name in INSTANCE_ID PATCH_TYPE PRED_BUCKET PRED_KEY RESULTS_BUCKET STDOUT_BUCKET STDOUT_KEY; do
    if [[ -z "${!name}" ]]; then
        echo "run_ec2.sh: missing required argument for ${name}" >&2
        exit 1
    fi
done

# Local path that run_instance.py writes its own log output to.
LOG_FILENAME='run.log'

PRED_URI="s3://${PRED_BUCKET}/${PRED_KEY}"
STDOUT_URI="s3://${STDOUT_BUCKET}/${STDOUT_KEY}"

# Copy the log file to S3 when this script exits.
upload_log() {
    local exit_code=$?
    echo "Uploading log to ${STDOUT_URI}"
    if aws s3 cp "${LOG_FILENAME}" "${STDOUT_URI}"; then
        echo "Uploaded log to ${STDOUT_URI}"
    else
        echo "run_ec2.sh: failed to upload log to ${STDOUT_URI}" >&2
    fi
    exit "${exit_code}"
}
trap upload_log EXIT

echo "Running instance ${INSTANCE_ID} ${PATCH_TYPE}, pred from ${PRED_URI} into results bucket ${RESULTS_BUCKET}, log into ${STDOUT_URI}"

uv run \
    ./scripts/run_instance.py \
    "${INSTANCE_ID}" \
    "${PATCH_TYPE}" \
    --pred-file "${PRED_URI}" \
    --parquet \
    --s3 \
    --bucket "${RESULTS_BUCKET}" \
    --log-file "${LOG_FILENAME}"
