#!/usr/bin/env bash
# Deploy (or update) the hosted evaluation API from aws/api.yaml.
#
# The workers check out this repository at the commit being deployed, so
# that commit has to be on GitHub first. The Lambda's list of valid
# instances is generated from dataset/ at deploy time.
#
# Environment (defaults in brackets):
#   AWS_PROFILE [admin-user]   AWS_REGION [us-east-1]
#   STACK [sbmdt-api]          BUDGET_EMAIL [none]
#   WORKER_SUBNET_ID, WORKER_SECURITY_GROUP_ID  (from aws/aws-resources.yaml;
#       default: the values in src/sbmdt/aws/env.py)
set -euo pipefail
cd "$(dirname "$0")/../.."

export AWS_PROFILE="${AWS_PROFILE:-admin-user}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
STACK="${STACK:-sbmdt-api}"
SUBNET="${WORKER_SUBNET_ID:-subnet-0d1aeaaad9a22c741}"
GROUP="${WORKER_SECURITY_GROUP_ID:-sg-09f9a76d742f8549d}"

COMMIT="$(git rev-parse HEAD)"
if [[ -z "$(git branch -r --contains "${COMMIT}")" ]]; then
    echo "deploy.sh: ${COMMIT} is not pushed; the workers could not fetch it" >&2
    exit 1
fi

uv run python - <<'PY'
import gzip, json, pathlib
ids = sorted(
    json.loads(line)['instance_id']
    for name in ('verified', 'multimodal')
    for line in gzip.open(f'dataset/{name}_labels.jsonl.gz', 'rt')
    if json.loads(line)['FAIL_TO_PASS']
)
pathlib.Path('aws/api/instances.json').write_text(json.dumps(ids))
print(len(ids), 'instances with FAIL_TO_PASS labels')
PY

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ARTIFACTS="${STACK}-artifacts-${ACCOUNT}"
aws s3api head-bucket --bucket "${ARTIFACTS}" 2>/dev/null \
    || aws s3 mb "s3://${ARTIFACTS}"

PACKAGED="$(mktemp --suffix=.yaml)"
trap 'rm -f "${PACKAGED}"' EXIT
aws cloudformation package \
    --template-file aws/api.yaml \
    --s3-bucket "${ARTIFACTS}" \
    --output-template-file "${PACKAGED}" >/dev/null

overrides=(
    "HarnessCommit=${COMMIT}"
    "WorkerSubnetId=${SUBNET}"
    "WorkerSecurityGroupId=${GROUP}"
)
if [[ -n "${BUDGET_EMAIL:-}" ]]; then
    overrides+=("BudgetEmail=${BUDGET_EMAIL}")
fi
aws cloudformation deploy \
    --stack-name "${STACK}" \
    --template-file "${PACKAGED}" \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides "${overrides[@]}"

aws cloudformation describe-stacks --stack-name "${STACK}" \
    --query 'Stacks[0].Outputs' --output table
