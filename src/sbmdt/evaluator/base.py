"""
Abstract base class for benchmark evaluators.

Defines the lifecycle interface (setup -> evaluate -> cleanup) that all
concrete evaluators must implement, and provides the final ``cleanup``
and ``run`` orchestration methods.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, final

import docker
from docker.models.containers import Container
from docker.models.images import Image

from sbmdt.env import DOCKERFILES_BASE
from sbmdt.pred import Pred
from sbmdt.utils import write_to_container

__all__ = [
    'Evaluator',
    'PatchType',
    'TestResult',
    'LABEL_KEY',
    'LABEL_VALUE',
]

log = logging.getLogger(__name__)

PATCH_FILE: Final[str] = '/tmp/model.patch'

LABEL_KEY = 'ca.maleknazn.sbmdt.managed'
LABEL_VALUE = 'true'


class PatchType(StrEnum):
    """The patch state under which a test was executed.

    Attributes:
        BEFORE_PATCH: Test run against the unmodified baseline.
        WITH_IMAGE: Test run with the patch applied via a custom image.
        WITHOUT_IMAGE: Test run with the patch applied without a custom image.
    """

    BEFORE_PATCH = 'before_patch'
    WITH_IMAGE = 'with_image'
    WITHOUT_IMAGE = 'without_image'
    GOLD = 'gold'


@dataclass(kw_only=True)
class TestResult:
    """Result of a single test case from a benchmark evaluation.

    Attributes:
        instance_id: Identifier of the benchmark instance that was evaluated.
        patch_type: The patch state under which the test was run.
        test_name: Name of the individual test case.
        passed: Whether the test case passed.
    """

    instance_id: str
    patch_type: PatchType
    agent_name: str
    test_name: str
    passed: bool


class Evaluator(ABC):
    """Abstract base for Docker-based benchmark evaluators.

    The constructor and :meth:`provision` (image build + container start)
    are shared by every subclass since this happens identically for all
    of them. Subclasses must implement :meth:`setup`, :meth:`evaluate`,
    :meth:`pre_cleanup`, and :meth:`post_cleanup`. The :meth:`cleanup`
    and :meth:`run` methods are final and handle container/image teardown
    and overall orchestration respectively.

    Attributes:
        instance_id: Identifier of the benchmark instance being evaluated.
        dockerfile_path: Path to the instance's Dockerfile, used by
            :meth:`provision` to build :attr:`image`.
        agent_name: Name of the agent that produced :attr:`pred`.
        image: The Docker image built or used by this evaluator.
        container: The running Docker container managed by this evaluator.
        patch_type: The patch state this evaluator is running under.
            Determines whether :meth:`run` calls :meth:`apply_patch`.
        pred: The model-generated patch to apply, or ``None`` when
            ``patch_type`` is :attr:`PatchType.BEFORE_PATCH`.
    """

    instance_id: str
    dockerfile_path: Path
    patch_type: PatchType
    agent_name: str
    pred: Pred | None
    image: Image | None
    container: Container | None

    def __init__(
        self,
        instance_id: str,
        patch_type: PatchType,
        agent_name: str,
        pred: Pred | None,
    ):
        """Initialize the evaluator for the given instance.

        Args:
            instance_id: Identifier for the benchmark instance. Used to
                locate the Dockerfile under ``DOCKERFILES_BASE`` and to
                name the resulting image and container.
            patch_type: The patch state this evaluator is running under.
            agent_name: Name of the agent that produced ``pred``.
            pred: The model-generated patch to apply, or ``None`` when
                ``patch_type`` is :attr:`PatchType.BEFORE_PATCH`.
        """
        self.instance_id = instance_id
        self.dockerfile_path = DOCKERFILES_BASE / instance_id / 'Dockerfile'
        self.patch_type = patch_type
        self.agent_name = agent_name
        self.pred = pred
        self.image = None
        self.container = None

    @final
    def provision(self) -> None:
        """Build the Docker image and start the container.

        Builds the image from ``self.dockerfile_path`` and starts a
        detached container from it, assigning ``self.image`` and
        ``self.container``. Called by :meth:`run` before :meth:`setup`.

        Docker image tags and container names must be lowercase, so
        ``self.instance_id`` is lowercased when building these resource
        names; ``self.instance_id`` itself is left untouched since it must
        still match the on-disk Dockerfile directory name exactly.
        """

        client = docker.from_env()
        resource_name = f'sbmdt-{self.instance_id}'.lower()
        self.image, _ = client.images.build(
            path=str(self.dockerfile_path.parent.resolve()),
            tag=f'{resource_name}:latest',
            labels={LABEL_KEY: LABEL_VALUE},
        )
        self.container = client.containers.run(
            self.image,
            command='/bin/bash',
            name=resource_name,
            stdin_open=True,
            tty=True,
            detach=True,
            labels={LABEL_KEY: LABEL_VALUE},
        )

    @abstractmethod
    def setup(self) -> None:
        """Perform evaluator-specific setup after the container is running.

        Called by :meth:`run` after the image has been built and the
        container started by :meth:`provision`. ``self.image`` and
        ``self.container`` are guaranteed to be set when this is called.
        """
        ...

    def apply_patch(self) -> None:
        """Apply ``self.pred.model_patch`` to ``/testbed`` via ``git apply``.

        Writes the patch to a temporary file inside the container and
        runs ``git apply`` against it from ``/testbed``.

        Raises:
            Exception: If the container has not been started, or if
                ``git apply`` exits non-zero.
        """

        if self.container is None:
            raise Exception('no container')
        assert self.pred is not None

        write_to_container(self.container, PATCH_FILE, self.pred.model_patch)

        exit_code, output = self.container.exec_run(
            f'git apply {PATCH_FILE}',
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        if exit_code != 0:
            raise Exception(
                f'Failed to apply patch for {self.instance_id}: '
                f'{output.decode()}'
            )

    @abstractmethod
    def evaluate(self) -> list[TestResult]:
        """Execute the benchmark and collect results.

        Returns:
            A list of :class:`TestResult` from the evaluation run.
        """
        ...

    @abstractmethod
    def pre_cleanup(self) -> None:
        """Hook called before container/image teardown.

        Runs inside a try/except in :meth:`cleanup`; exceptions are logged
        but do not abort cleanup.
        """
        ...

    @abstractmethod
    def post_cleanup(self) -> None:
        """Hook called after container/image teardown.

        Runs inside a try/except in :meth:`cleanup`; exceptions are logged
        but do not abort cleanup.
        """
        ...

    @final
    def cleanup(self):
        """Stop and remove the container and image.

        Calls :meth:`pre_cleanup` first and :meth:`post_cleanup` last.
        Each step is wrapped in its own try/except so a failure in one
        step does not prevent the remaining steps from running.
        """

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
    def run(self) -> list[TestResult]:
        """Run the full evaluation lifecycle.

        Stages: :meth:`provision` (build image, start container), then
        :meth:`setup`, then :meth:`apply_patch` (only when
        ``self.patch_type`` is not :attr:`PatchType.BEFORE_PATCH`), then
        :meth:`evaluate`, then :meth:`cleanup`.
        """
        self.provision()
        self.setup()
        if self.patch_type != PatchType.BEFORE_PATCH:
            self.apply_patch()
        results = self.evaluate()
        self.cleanup()
        return results
