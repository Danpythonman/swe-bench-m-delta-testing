#!/usr/bin/env bash
#
# Sync test artifacts from S3 to the local s3-sync directory.
#
# Downloads three S3 buckets (test-results, predictions, and stdout logs)
# into BASE/s3-sync, relative to this script's location (BASE/aws/).
# Paths are resolved from the script's own directory, so it can be run
# from any working directory.
#
# Requires: bash, AWS CLI v2 with configured credentials.
# Usage: ./sync-s3.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC_DIR="$SCRIPT_DIR/../s3-sync"

aws s3 sync s3://sbmdt-test-results "$SYNC_DIR/test-results"
aws s3 sync s3://sbmdt-preds        "$SYNC_DIR/preds"
aws s3 sync s3://sbmdt-stdout       "$SYNC_DIR/stdout"
