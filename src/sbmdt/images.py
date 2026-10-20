"""The benchmark base images every Dockerfile is built ``FROM``.

Each instance's Dockerfile starts::

    ARG REGISTRY=docker.io
    FROM ${REGISTRY}/swebench/sweb.eval.x86_64.<name>@sha256:<digest>

The image is pinned by digest, so a rerun gets the exact environment the
results came from even if SWE-bench later re-pushes the tag. The registry
is a build argument: Docker Hub by default, so the harness runs with
nothing but Docker, or the project's ECR pull-through cache, which mirrors
Docker Hub and spares EC2 batches its anonymous pull limit. Both serve the
same digests. :func:`registry` reads the choice from ``SBMDT_REGISTRY``.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from typing import Final

from sbmdt.env import DOCKERFILES_BASE

__all__ = [
    'DOCKER_HUB',
    'ECR_DOCKER_HUB',
    'REGISTRY_ENV',
    'BaseImage',
    'base_image',
    'docker_hub_digest',
    'dockerfile_header',
    'parse_base_image',
    'registry',
]

DOCKER_HUB: Final[str] = 'docker.io'

# The project's ECR pull-through cache for Docker Hub (see aws/run_ec2.sh).
ECR_DOCKER_HUB: Final[str] = (
    '607869540801.dkr.ecr.us-east-1.amazonaws.com/docker-hub'
)

REGISTRY_ENV: Final[str] = 'SBMDT_REGISTRY'

# A FROM line in any of the forms the Dockerfiles have used: a literal
# registry or ${REGISTRY}, then the repository, then a tag or a digest.
_FROM: Final[re.Pattern[str]] = re.compile(
    r'^FROM\s+(?:\$\{REGISTRY\}|\S+?/docker-hub|docker\.io)/'
    r'(?P<repository>swebench/[^:@\s]+)'
    r'(?::(?P<tag>[^@\s]+))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?\s*$',
    re.MULTILINE,
)

# Manifest types Docker Hub may answer with; the digest is of whichever
# one it returns, the same one ``docker pull`` resolves.
_MANIFEST_TYPES: Final[str] = ', '.join(
    [
        'application/vnd.oci.image.index.v1+json',
        'application/vnd.docker.distribution.manifest.list.v2+json',
        'application/vnd.oci.image.manifest.v1+json',
        'application/vnd.docker.distribution.manifest.v2+json',
    ]
)


@dataclass(frozen=True)
class BaseImage:
    """A Dockerfile's base image: repository plus a tag and/or digest.

    Attributes:
        repository: e.g. ``swebench/sweb.eval.x86_64.<name>``.
        tag: e.g. ``latest``, or ``None`` when only the digest is given.
        digest: e.g. ``sha256:...``, or ``None`` when not yet pinned.
    """

    repository: str
    tag: str | None
    digest: str | None

    def reference(self, registry: str) -> str:
        """The full image reference in ``registry``, by digest if pinned.

        Args:
            registry: e.g. :data:`DOCKER_HUB` or :data:`ECR_DOCKER_HUB`.
        """
        suffix = f'@{self.digest}' if self.digest else f':{self.tag}'
        return f'{registry}/{self.repository}{suffix}'


def registry() -> str:
    """The registry to pull base images from: ``$SBMDT_REGISTRY`` or
    Docker Hub.
    """
    return os.environ.get(REGISTRY_ENV) or DOCKER_HUB


def parse_base_image(dockerfile: str) -> BaseImage:
    """Find the base image in a Dockerfile's text.

    Args:
        dockerfile: The Dockerfile's contents.

    Returns:
        Its base image.

    Raises:
        ValueError: If no FROM line names a SWE-bench image.
    """
    m = _FROM.search(dockerfile)
    if m is None:
        raise ValueError('Dockerfile has no FROM line for a SWE-bench image')
    return BaseImage(m['repository'], m['tag'], m['digest'])


def base_image(instance_id: str, in_registry: str | None = None) -> str:
    """The full reference of the image an instance's Dockerfile builds on.

    Args:
        instance_id: The instance whose ``dockerfiles/<id>/Dockerfile`` to
            read.
        in_registry: The registry; :func:`registry` if omitted.

    Returns:
        e.g. ``docker.io/swebench/sweb.eval.x86_64.<name>@sha256:...``.
    """
    dockerfile = (DOCKERFILES_BASE / instance_id / 'Dockerfile').read_text()
    image = parse_base_image(dockerfile)
    return image.reference(in_registry or registry())


def dockerfile_header(image: BaseImage) -> str:
    """The ARG and FROM lines that start a Dockerfile.

    Args:
        image: The base image, pinned (``digest``) or not (``tag``).
    """
    return (
        f'ARG REGISTRY={DOCKER_HUB}\nFROM {image.reference("${REGISTRY}")}\n'
    )


def docker_hub_digest(repository: str, tag: str = 'latest') -> str:
    """The digest Docker Hub serves for ``repository:tag`` right now.

    Asks with a HEAD request, which downloads nothing and does not count
    toward Docker Hub's pull limit.

    Args:
        repository: e.g. ``swebench/sweb.eval.x86_64.<name>``.
        tag: The tag to resolve.

    Returns:
        e.g. ``sha256:...``.

    Raises:
        urllib.error.HTTPError: If Docker Hub has no such image.
    """
    token_url = (
        'https://auth.docker.io/token?service=registry.docker.io'
        f'&scope=repository:{repository}:pull'
    )
    with urllib.request.urlopen(token_url, timeout=30) as response:
        token = json.load(response)['token']
    request = urllib.request.Request(
        f'https://registry-1.docker.io/v2/{repository}/manifests/{tag}',
        method='HEAD',
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': _MANIFEST_TYPES,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.headers['Docker-Content-Digest']
