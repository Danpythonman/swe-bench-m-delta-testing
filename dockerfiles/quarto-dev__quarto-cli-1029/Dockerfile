ARG REGISTRY=docker.io
FROM ${REGISTRY}/swebench/sweb.eval.x86_64.quarto-dev_1776_quarto-cli-1029@sha256:1349f4fb946b1b5cb7e9777099badb018b78d6486bbe5032706bb20e0ef13a83

WORKDIR /install
# Install docker cli
# Add Docker's official GPG key:
RUN apt-get update
RUN apt-get install -y ca-certificates curl
RUN install -m 0755 -d /etc/apt/keyrings
RUN curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
RUN chmod a+r /etc/apt/keyrings/docker.asc

# Add the repository to Apt sources:
RUN echo   "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu   $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" |   tee /etc/apt/sources.list.d/docker.list > /dev/null
RUN apt-get update && apt-get install -y docker-ce-cli

# Install required dependencies for SWE-bench (verification)
RUN apt-get install -y -V ca-certificates lsb-release wget
RUN wget https://apache.jfrog.io/artifactory/arrow/$(lsb_release --id --short | tr 'A-Z' 'a-z')/apache-arrow-apt-source-latest-$(lsb_release --codename --short).deb
RUN apt-get update
RUN apt-get install -y -V ./apache-arrow-apt-source-latest-$(lsb_release --codename --short).deb

# Install SWE repo
RUN git clone https://github.com/princeton-nlp/SWE-bench.git /SWE-bench
WORKDIR /SWE-bench
RUN apt-get install -y python3-venv
RUN python3 -m venv .venv
RUN . .venv/bin/activate      &&     pip install --upgrade pip &&     pip install . 

WORKDIR /testbed
