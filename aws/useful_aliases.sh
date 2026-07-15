#!/usr/bin/env bash
#
# useful_aliases.sh
#
# A collection of shell functions that wrap common AWS CLI commands.
#
# Setup:
#   1. Make sure the AWS CLI (v2) is installed and configured (`aws configure`).
#   2. Source this file in your shell: `source useful_aliases.sh`
#      (add that line to ~/.bashrc or ~/.zshrc to load it automatically)
#   3. Call any function below by name, e.g. `list-instances`
#
# Note: functions use `aws --query` (JMESPath) and `--output table` for
# readable results. Change `table` to `json` if you want raw output for
# scripting.
#
# Profiles:
#   Every function accepts an AWS CLI profile name as its FIRST argument.
#   If omitted, it falls back to the "default" profile.
#   Examples:
#     list-instances                # uses "default" profile
#     list-instances my-profile     # uses "my-profile"
#     start-instance my-profile i-0123456789abcdef0
#   Run `aws configure list-profiles` to see available profile names.

# List all EC2 instances with ID, Name tag, and current state.
# Usage: list-instances [profile]
list-instances() {
    aws ec2 describe-instances --profile "${1:-default}" \
        --query 'Reservations[].Instances[].{ID:InstanceId,Name:Tags[?Key==`Name`]|[0].Value,State:State.Name}' \
        --output table
}

# List only running EC2 instances.
# Usage: list-running-instances [profile]
list-running-instances() {
    aws ec2 describe-instances --profile "${1:-default}" \
        --filters "Name=instance-state-name,Values=running" \
        --query 'Reservations[].Instances[].{ID:InstanceId,Name:Tags[?Key==`Name`]|[0].Value,Type:InstanceType,PublicIP:PublicIpAddress}' \
        --output table
}

# Start an EC2 instance. Usage: start-instance [profile] <instance-id>
start-instance() {
    aws ec2 start-instances --profile "${1:-default}" --instance-ids "$2"
}

# Stop an EC2 instance. Usage: stop-instance [profile] <instance-id>
stop-instance() {
    aws ec2 stop-instances --profile "${1:-default}" --instance-ids "$2"
}

# List security groups with their ID, name, and description.
# Usage: list-security-groups [profile]
list-security-groups() {
    aws ec2 describe-security-groups --profile "${1:-default}" \
        --query 'SecurityGroups[].{ID:GroupId,Name:GroupName,Description:Description}' --output table
}

# List all S3 buckets with creation date.
# Usage: list-buckets [profile]
list-buckets() {
    aws s3api list-buckets --profile "${1:-default}" \
        --query 'Buckets[].{Name:Name,Created:CreationDate}' --output table
}

# Show total size and object count of a bucket.
# Usage: bucket-size [profile] <bucket-name>
bucket-size() {
    aws s3 ls "s3://$2" --profile "${1:-default}" --recursive --summarize | tail -n 2
}

# List IAM users with creation date.
# Usage: list-users [profile]
list-users() {
    aws iam list-users --profile "${1:-default}" \
        --query 'Users[].{Name:UserName,Created:CreateDate}' --output table
}

# List IAM roles with creation date.
# Usage: list-roles [profile]
list-roles() {
    aws iam list-roles --profile "${1:-default}" \
        --query 'Roles[].{Name:RoleName,Created:CreateDate}' --output table
}

# List VPCs with ID, CIDR block, and whether it's the default VPC.
# Usage: list-vpcs [profile]
list-vpcs() {
    aws ec2 describe-vpcs --profile "${1:-default}" \
        --query 'Vpcs[].{ID:VpcId,CIDR:CidrBlock,Default:IsDefault}' --output table
}

# List Lambda functions with runtime and last modified date.
# Usage: list-lambdas [profile]
list-lambdas() {
    aws lambda list-functions --profile "${1:-default}" \
        --query 'Functions[].{Name:FunctionName,Runtime:Runtime,LastModified:LastModified}' --output table
}

# List CloudWatch log groups.
# Usage: list-log-groups [profile]
list-log-groups() {
    aws logs describe-log-groups --profile "${1:-default}" \
        --query 'logGroups[].{Name:logGroupName,StoredBytes:storedBytes}' --output table
}

# Tail the latest log stream in a log group.
# Usage: tail-logs [profile] <log-group-name>
tail-logs() {
    local profile="${1:-default}"
    local log_group="$2"
    local latest_stream
    latest_stream=$(aws logs describe-log-streams --profile "$profile" --log-group-name "$log_group" \
        --order-by LastEventTime --descending --limit 1 --query 'logStreams[0].logStreamName' --output text)
    aws logs get-log-events --profile "$profile" --log-group-name "$log_group" --log-stream-name "$latest_stream" --output table
}

# Show the currently authenticated identity (account, user/role, ARN).
# Usage: whoami-aws [profile]
whoami-aws() {
    aws sts get-caller-identity --profile "${1:-default}" --output table
}

get-sbmdt-stdout-object() {
    aws s3 cp "s3://sbmdt-stdout/${1}" -
}
