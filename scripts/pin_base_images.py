"""Pin every Dockerfile's base image by digest, with a registry argument.

Rewrites the start of ``dockerfiles/<id>/Dockerfile``, whatever its old
form (a literal ECR or Docker Hub registry, a tag), to::

    ARG REGISTRY=docker.io
    FROM ${REGISTRY}/swebench/sweb.eval.x86_64.<name>@sha256:<digest>

and leaves the rest of the file as it is. The digest is the one Docker Hub
serves for the image's tag right now (a HEAD request: nothing is
downloaded and Docker Hub's pull limit is not touched). Dockerfiles that
are already pinned are left alone unless ``--repin`` is given, so a later
re-push of the tag upstream never silently changes an environment.

Usage:
    uv run scripts/pin_base_images.py                 # pin what is unpinned
    uv run scripts/pin_base_images.py --check         # list, change nothing
    uv run scripts/pin_base_images.py --instance <id> ...
    uv run scripts/pin_base_images.py --repin         # refresh every digest
"""

from __future__ import annotations

import argparse
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from sbmdt.env import DOCKERFILES_BASE
from sbmdt.images import docker_hub_digest, dockerfile_header, parse_base_image
from sbmdt.log import setup_logging

log = logging.getLogger(__name__)

# Everything up to and including the FROM line: blank lines, and the ARG
# line of an already-converted Dockerfile.
_HEADER = re.compile(r'\A(?:\s*\n|ARG [^\n]*\n)*FROM [^\n]*\n')


def pin(dockerfile: Path, repin: bool, check: bool) -> str:
    """Pin one Dockerfile; return what happened, for the summary."""
    text = dockerfile.read_text()
    image = parse_base_image(text)
    if image.digest and not repin:
        return 'already pinned'
    if check:
        return 'unpinned'
    digest = docker_hub_digest(image.repository, image.tag or 'latest')
    if digest == image.digest:
        return 'unchanged'
    pinned = replace(image, tag=None, digest=digest)
    m = _HEADER.match(text)
    if m is None:
        raise ValueError(f'{dockerfile}: FROM is not the first instruction')
    dockerfile.write_text(dockerfile_header(pinned) + text[m.end() :])
    return 'repinned' if image.digest else 'pinned'


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(
        description='Pin every Dockerfile base image by digest.'
    )
    parser.add_argument(
        '--instance', nargs='+', help='Only these instance IDs.'
    )
    parser.add_argument(
        '--repin',
        action='store_true',
        help="Replace existing digests with the tag's current one.",
    )
    parser.add_argument(
        '--check',
        action='store_true',
        help='Only report which Dockerfiles are unpinned.',
    )
    parser.add_argument('--workers', type=int, default=8)
    return parser.parse_args()


def main() -> None:
    """Pin the selected Dockerfiles and summarize."""
    args = parse_args()
    setup_logging(level=logging.INFO)
    if args.instance:
        files = [DOCKERFILES_BASE / i / 'Dockerfile' for i in args.instance]
    else:
        files = sorted(DOCKERFILES_BASE.glob('*/Dockerfile'))
    repin: bool = args.repin
    check: bool = args.check

    def one(dockerfile: Path) -> str:
        return pin(dockerfile, repin, check)

    with ThreadPoolExecutor(args.workers) as pool:
        outcomes = list(pool.map(one, files))
    counts: dict[str, int] = {}
    for dockerfile, outcome in zip(files, outcomes, strict=True):
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome in ('unpinned', 'repinned'):
            log.info(f'{dockerfile.parent.name}: {outcome}')
    log.info(f'{len(files)} Dockerfiles: {counts}')


if __name__ == '__main__':
    main()
