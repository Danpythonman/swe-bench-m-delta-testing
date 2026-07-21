packer {
    required_plugins {
        amazon = {
            source  = "github.com/hashicorp/amazon"
            version = ">= 1.3.0"
        }
    }
}

variable "region" {
    type    = string
    default = "us-east-1"
}

variable "instance_type" {
    type    = string
    default = "t2.medium"
}

variable "repo_url" {
    type    = string
    default = "https://github.com/Danpythonman/swe-bench-m-delta-testing.git"
}

variable "python_version" {
    type    = string
    default = "3.13"
}

source "amazon-ebs" "al" {
    region        = var.region
    instance_type = var.instance_type
    ssh_username  = "ec2-user"
    ami_name      = "al-sbmdt-ami-{{timestamp}}"

    launch_block_device_mappings {
        device_name           = "/dev/xvda"
        volume_size           = 8
        volume_type           = "gp3"
        delete_on_termination = true
    }

    source_ami_filter {
        filters = {
            name                = "al2023-ami-*-x86_64"
            root-device-type    = "ebs"
            virtualization-type = "hvm"
        }
        owners      = ["137112412989"] # Amazon
        most_recent = true
    }
}

build {
    sources = ["source.amazon-ebs.al"]

    # Docker + git
    provisioner "shell" {
        inline = [
            "sudo dnf install -y docker git",
            "sudo systemctl enable docker",
            "sudo systemctl start docker",
        ]
    }

    # AWS SSM agent
    provisioner "shell" {
        inline = [
            "sudo dnf install -y amazon-ssm-agent",
            "sudo systemctl enable amazon-ssm-agent",
            "sudo systemctl start amazon-ssm-agent",
        ]
    }

    # Create ssm-user, and a shared group, sbmdt-group, for /opt access
    provisioner "shell" {
        inline = [
            "sudo useradd -m ssm-user || true",
            "sudo usermod -aG docker ssm-user",
            "sudo groupadd sbmdt-group || true",
            "sudo usermod -aG sbmdt-group ssm-user",
            "sudo usermod -aG sbmdt-group ec2-user",
        ]
    }

    # Create /opt/sbmdt, owned by root:sbmdt-group, group-writable, with
    # setgid so files/dirs created inside inherit the sbmdt-group group
    provisioner "shell" {
        inline = [
            "sudo mkdir -p /opt/sbmdt",
            "sudo chown root:sbmdt-group /opt/sbmdt",
            "sudo chmod 2775 /opt/sbmdt",
        ]
    }

    # Install uv + Python globally, as root, into /usr/local/bin.
    # Python versions go to /opt/uv/python (shared, group-readable) so
    # ssm-user's later `uv sync` finds the same interpreter root installed.
    provisioner "shell" {
        inline = [
            "sudo bash -c 'curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=\"/usr/local/bin\" sh'",
            "sudo mkdir -p /opt/uv/python",
            "sudo chown root:sbmdt-group /opt/uv/python",
            "sudo chmod 2775 /opt/uv/python",
            "echo 'export UV_PYTHON_INSTALL_DIR=/opt/uv/python' | sudo tee /etc/profile.d/uv.sh",
            "sudo chmod 644 /etc/profile.d/uv.sh",
            "sudo UV_PYTHON_INSTALL_DIR=/opt/uv/python uv python install ${var.python_version}",
        ]
    }

    # Clone the repo and sync dependencies.
    #
    # Note that uv sync runs as ssm-user.
    #
    # This means to run Python code in the instance, you should be logged in as
    # ssm-user.
    provisioner "shell" {
        inline = [
            "sudo -i -u ssm-user bash -c 'GIT_TERMINAL_PROMPT=0 git clone --depth 1 --single-branch --branch aws ${var.repo_url} /opt/sbmdt'",
            "sudo -i -u ssm-user bash -c 'cd /opt/sbmdt && uv sync --frozen || uv sync'",
        ]
    }

    # Clean up before the AMI snapshot is taken
    provisioner "shell" {
        inline = [
            "sudo dnf clean all",
            "sudo rm -rf /tmp/* /var/tmp/*",
            "sudo rm -f /etc/ssh/ssh_host_*",
            "sudo truncate -s 0 /etc/machine-id",
            "sudo rm -rf /var/lib/cloud/instances/*",
            "sudo cloud-init clean --logs",
            "history -c",
            "sudo bash -c 'history -c'",
        ]
    }
}
