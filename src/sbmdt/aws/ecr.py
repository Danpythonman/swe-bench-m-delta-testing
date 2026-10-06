"""The ECR pull-through cache that serves the benchmark base images."""

from __future__ import annotations

import base64
import subprocess
from typing import Any, cast

import boto3

from sbmdt.env import DOCKERFILES_BASE

__all__ = [
    'base_image',
    'docker_login',
    'is_cached',
]


def base_image(instance_id: str) -> str:
    """Return the image the instance's Dockerfile is built ``FROM``.

    Args:
        instance_id: A Verified instance ID.

    Returns:
        The full image reference, e.g. ``<registry>/docker-hub/...:latest``.
    """
    dockerfile = (DOCKERFILES_BASE / instance_id / 'Dockerfile').read_text()
    first = dockerfile.splitlines()[0]
    return first.removeprefix('FROM ').strip()


def is_cached(ecr: Any, image: str) -> bool:
    """Return True when ECR already holds ``image``.

    Args:
        ecr: A boto3 ECR client.
        image: A full image reference in the ECR registry.

    Returns:
        Whether the cached repository exists and has the tag.
    """
    repository, _, tag = image.split('/', 1)[1].rpartition(':')
    try:
        ecr.describe_images(
            repositoryName=repository, imageIds=[{'imageTag': tag}]
        )
    except Exception:
        return False
    return True


def docker_login(session: boto3.Session, registry: str) -> None:
    """Log the local Docker daemon into the ECR registry.

    Args:
        session: A boto3 session for the account.
        registry: The registry host.
    """
    ecr = cast(Any, session.client('ecr'))  # pyright: ignore[reportUnknownMemberType]
    token = cast(dict[str, Any], ecr.get_authorization_token())
    data = cast(str, token['authorizationData'][0]['authorizationToken'])
    password = base64.b64decode(data).decode().split(':', 1)[1]
    subprocess.run(
        ['docker', 'login', '--username', 'AWS', '--password-stdin', registry],
        input=password,
        text=True,
        check=True,
        capture_output=True,
    )
