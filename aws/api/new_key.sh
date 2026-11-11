#!/usr/bin/env bash
# Issue an API key for the hosted evaluation API and print it.
#
#   aws/api/new_key.sh <name>     e.g. aws/api/new_key.sh msr-reviewer-1
set -euo pipefail
NAME="${1:?usage: new_key.sh <name>}"
export AWS_PROFILE="${AWS_PROFILE:-admin-user}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
STACK="${STACK:-sbmdt-api}"

PLAN="$(aws cloudformation describe-stacks --stack-name "${STACK}" \
    --query "Stacks[0].Outputs[?OutputKey=='UsagePlanId'].OutputValue" \
    --output text)"
KEY_ID="$(aws apigateway create-api-key --name "${STACK}-${NAME}" --enabled \
    --query id --output text)"
aws apigateway create-usage-plan-key --usage-plan-id "${PLAN}" \
    --key-id "${KEY_ID}" --key-type API_KEY >/dev/null
echo "key id: ${KEY_ID}"
echo "key:    $(aws apigateway get-api-key --api-key "${KEY_ID}" \
    --include-value --query value --output text)"
