import logging
from abc import ABC, abstractmethod
from typing import final

from docker.models.containers import Container
from docker.models.images import Image

__all__ = ['Evaluator']
log = logging.getLogger(__name__)


class Evaluator(ABC):
    image: Image | None
    container: Container | None

    @abstractmethod
    def setup(self): ...

    @abstractmethod
    def evaluate(self): ...

    @abstractmethod
    def pre_cleanup(self): ...

    @abstractmethod
    def post_cleanup(self): ...

    @final
    def cleanup(self):
        try:
            self.pre_cleanup()
        except Exception:
            log.error('Error running pre-cleanup hook')

        if self.container:
            try:
                log.info(f'Stopping container {self.container.name}')
                self.container.stop()
                log.info(f'Stopped container {self.container.name}')
            except Exception as e:
                log.error(f'Failed to stop container: {e}')
            try:
                log.info(f'Removing container {self.container.name}')
                self.container.remove()
                log.info(f'Removed container {self.container.name}')
            except Exception as e:
                log.error(f'Failed to remove container: {e}')
        else:
            log.warning('Container did not exist')

        if self.image:
            try:
                log.info(f'Removing image {self.image.tags}')
                self.image.remove()
                log.info(f'Removed image {self.image.tags}')
            except Exception as e:
                log.error(f'Failed to remove image: {e}')
        else:
            log.warning('Image did not exist')

        try:
            self.post_cleanup()
        except Exception:
            log.error('Error running post-cleanup hook')

    @final
    def run(self):
        self.setup()
        self.evaluate()
        self.cleanup()
