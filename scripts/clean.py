import logging

import docker
import docker.errors

from sbmdt.evaluator.base import LABEL_KEY
from sbmdt.log import setup_logging

log = logging.getLogger(__name__)

LABEL_FILTER = f'{LABEL_KEY}=true'


def cleanup() -> None:
    """Stop and remove all containers and images that were created by our
    system.
    """

    setup_logging()

    client = docker.from_env()

    containers = client.containers.list(
        all=True,
        filters={'label': LABEL_FILTER},
    )
    log.info(f'Found {len(containers)} managed container(s)')

    for container in containers:
        log.info(f'Stopping container {container.name} ({container.short_id})')
        try:
            container.stop()
        except docker.errors.APIError as exc:
            log.warning(f'Failed to stop {container.name}: {exc}')

        log.info(f'Removing container {container.name} ({container.short_id})')
        try:
            container.remove(force=True)
        except docker.errors.APIError as exc:
            log.warning(f'Failed to remove {container.name}: {exc}')

    images = client.images.list(filters={'label': LABEL_FILTER})
    log.info(f'Found {len(images)} managed image(s)')

    for image in images:
        tags = image.tags or [image.short_id]
        log.info(f'Removing image {", ".join(tags)}')
        try:
            client.images.remove(image.id, force=True)
        except docker.errors.APIError as exc:
            log.warning(f'Failed to remove image {image.short_id}: {exc}')

    log.info('Cleanup complete')


def main():
    cleanup()


if __name__ == '__main__':
    main()
