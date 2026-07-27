# Distributed Processing of the Pipeline via AWS

This directory holds the infrastructure for running the evaluation pipeline
on disposable EC2 instances instead of locally, so that many instances can be
evaluated in parallel.

At a high level:

1. A **Packer** template builds an AMI that already has Docker, `uv`, and a
   clone of this repo baked in.
2. A **CloudFormation** stack builds the networking (VPC/subnet) and IAM
   permissions the instances need, without opening any inbound ports.
3. The [`scripts/run_ec2.py`](../scripts/run_ec2.py) orchestrator launches an
   instance from the AMI into that network, waits for it to come up, runs a
   command on it over SSM (no SSH needed), and terminates the instance
   afterward.

## Files in this directory

| File | Purpose |
| --- | --- |
| [`sbmdt-worker-ami.pkr.hcl`](sbmdt-worker-ami.pkr.hcl) | Packer template that builds the worker AMI (Amazon Linux 2023 + Docker + SSM Agent + `uv` + a clone of this repo). |
| [`aws-resources.yaml`](aws-resources.yaml) | CloudFormation template for the VPC, subnet, security group, and IAM instance role/profile that workers run in. |
| [`run_ec2.sh`](run_ec2.sh) | Snippet showing the command run *inside* an instance (via SSM) to sanity-check the environment. |
| [`useful_aliases.sh`](useful_aliases.sh) | Shell function(s) for poking at EC2 state from your local machine while debugging. |

## Requirements

We use two CLIs here:

- **Packer** builds AMIs without having to manually run setup commands on an
  instance and snapshot it by hand.

  Install instructions: <https://developer.hashicorp.com/packer/install>

- **AWS CLI** is used to create/update the CloudFormation stack and to talk
  to EC2/SSM.

  Install instructions: <https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html>

  We also use the CLI's **SSM plugin** to open interactive sessions on
  instances (there's no SSH access; see [Networking model](#networking-model)
  below).

  Install instructions: <https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html>

You'll also need an AWS CLI profile with sufficient permissions to create
EC2/IAM/CloudFormation resources. The commands below (and
[`scripts/run_ec2.py`](../scripts/run_ec2.py)) assume a profile named
`admin-user`:

```bash
aws configure --profile admin-user
```

## Networking model

Instances get a public IPv4/IPv6 address (needed for outbound package
installs) but are **not reachable inbound** from the internet. The security
group defined in `aws-resources.yaml` has zero ingress rules and only allows
outbound HTTP/HTTPS. The only way onto an instance is AWS Systems Manager
Session Manager, whose agent on the instance initiates the connection
*outbound*, so no inbound port needs to be open.

## One-time setup

### 1. Build the CloudFormation stack

This creates the VPC, subnet, security group, and IAM instance
role/profile that instances launch into. It does **not** create the S3
buckets referenced in the IAM policy (`sbmdt-preds`, `sbmdt-test-results`,
`sbmdt-stdout`).

```bash
aws cloudformation create-stack \
  --stack-name sbmdt-stack \
  --template-body file://aws/aws-resources.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --profile admin-user
```

Check on it (creation takes a minute or two):

```bash
aws cloudformation describe-stacks --stack-name sbmdt-stack --profile admin-user
```

Once `StackStatus` is `CREATE_COMPLETE`, grab the outputs you'll need later
(subnet ID, security group ID, instance profile ARN):

```bash
aws cloudformation describe-stacks \
  --stack-name sbmdt-stack \
  --query 'Stacks[0].Outputs' \
  --profile admin-user
```

To tear the stack down later:

```bash
aws cloudformation delete-stack --stack-name sbmdt-stack --profile admin-user
```

### 2. Build the worker AMI

The first time you use this template, initialize its plugins:

```bash
packer init aws/sbmdt-worker-ami.pkr.hcl
```

Then build the AMI:

```bash
packer build aws/sbmdt-worker-ami.pkr.hcl
```

This provisions a fresh Amazon Linux 2023 instance, installs Docker and the
SSM Agent, creates an `ssm-user` (in a shared `sbmdt-group` group with
`ec2-user`), installs `uv` and Python (see `python_version` variable) into
`/opt/uv/python`, clones the `aws` branch of this repo into `/opt/sbmdt`
(owned by `root:sbmdt-group`, group-writable) and runs `uv sync` as
`ssm-user`, then cleans up and snapshots an AMI.

Useful variables (override with `-var`, e.g. `-var 'region=us-west-2'`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `region` | `us-east-1` | Region to build the AMI in. |
| `instance_type` | `t2.medium` | Build-time instance type (not the type instances run as later). |
| `repo_url` | this repo's GitHub URL | Repo cloned into `/opt/sbmdt` on the image. |
| `python_version` | `3.13` | Python version installed via `uv`. |

Packer prints the resulting AMI ID when the build finishes, which is needed in
the next step.

### 3. Wire the AMI and stack outputs into the orchestrator

[`scripts/run_ec2.py`](../scripts/run_ec2.py) launches instances using a few
hardcoded constants at the top of the file. After (re)building the AMI or
the CloudFormation stack, update them to match:

```python
IMAGE_ID = 'ami-...'                 # AMI ID printed by `packer build`
SUBNET_ID = 'subnet-...'             # SubnetId stack output
SECURITY_GROUP_ID = 'sg-...'         # SecurityGroupId stack output
INSTANCE_PROFILE_ARN = 'arn:...'     # InstanceProfileArn stack output
```

## Day-to-day usage

### Running work on an instance

`scripts/run_ec2.py` is the orchestrator: it launches an instance from
`IMAGE_ID`, waits for it to reach `running`, waits for the SSM Agent to
register, sends a shell command to it via SSM, and terminates the instance on
exit.

```bash
uv run python scripts/run_ec2.py
```

[`run_ec2.sh`](run_ec2.sh) is the actual command that gets run on the
instance.

### Connecting to an instance interactively

If you need to poke around on a running instance instead of going through
`run_ec2.py`, start a session with the SSM plugin:

```bash
aws ssm start-session --target <instance-id> --profile admin-user
```

You'll land as `ssm-user`; `/opt/sbmdt` is where the repo lives.

### Debugging helpers

[`useful_aliases.sh`](useful_aliases.sh) has shell functions for checking on
instance state without typing out the full AWS CLI query each time. Source
it in your shell:

```bash
source aws/useful_aliases.sh
```

```bash
list-instances   # table of instance ID, Name tag, and state for all instances
```

## Notes

- `create_instance` in `run_ec2.py` currently passes `KeyName='sbmdt-debug'`
  to attach an SSH key pair for debugging access. This is meant to go away
  once instance access is fully handled through SSM.

- Everything in `run_ec2.py` and the `create-stack`/`describe-stacks`
  examples above assumes an AWS CLI profile named `admin-user`. Change the
  `profile_name`/`--profile` if yours is named differently.

- The IAM role created by the stack only grants access to three specific S3
  buckets (`sbmdt-preds`, `sbmdt-test-results`, `sbmdt-stdout`) plus SSM and
  CloudWatch Agent managed policies.
