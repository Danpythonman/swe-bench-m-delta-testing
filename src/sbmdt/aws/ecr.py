"""The ECR pull-through cache that serves the benchmark base images."""

from __future__ import annotations

import base64
import subprocess
from typing import Any, cast

import boto3

__all__ = [
    'docker_login',
    'is_cached',
]


def is_cached(ecr: Any, image: str) -> bool:
    """Return True when ECR already holds ``image``.

    Args:
        ecr: A boto3 ECR client.
        image: A full image reference in the ECR registry, by tag
            (``...:latest``) or by digest (``...@sha256:...``).

    Returns:
        Whether the cached repository exists and has the tag or digest.
    """
    path = image.split('/', 1)[1]
    if '@' in path:
        repository, digest = path.split('@', 1)
        image_id = {'imageDigest': digest}
    else:
        repository, _, tag = path.rpartition(':')
        image_id = {'imageTag': tag}
    try:
        ecr.describe_images(repositoryName=repository, imageIds=[image_id])
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
