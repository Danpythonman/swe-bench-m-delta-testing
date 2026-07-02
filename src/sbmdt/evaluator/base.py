"""
Abstract base class for benchmark evaluators.

Defines the lifecycle interface (setup -> evaluate -> cleanup) that all
concrete evaluators must implement, and provides the final ``cleanup``
and ``run`` orchestration methods.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, final

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
    'TestResultsFilename',
    'LABEL_KEY',
    'LABEL_VALUE',
]

log = logging.getLogger(__name__)

PATCH_FILE: Final[str] = '/tmp/model.patch'

LABEL_KEY = 'ca.maleknazn.sbmdt.managed'
LABEL_VALUE = 'true'

_TIMESTAMP_FMT: Final[str] = '%Y-%m-%d_%H-%M-%SZ'
_TIMESTAMP_RE: Final[str] = r'\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}Z'
_FILENAME_RE: Final[re.Pattern[str]] = re.compile(
    rf'^(?P<instance_id>.+)-(?P<patch_type>.+)-(?P<agent_name>.+)-'
    rf'(?P<timestamp>{_TIMESTAMP_RE})$'
)


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


def factory(items: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON-safe dict from dataclass fields.

    Intended for use as the ``dict_factory`` argument to
    :func:`dataclasses.asdict`. Converts any :class:`datetime.datetime`
    values to ISO 8601 strings; all other values are passed through
    unchanged.

    Args:
        items: A list of (field_name, value) tuples, as produced by
            ``dataclasses.asdict``.

    Returns:
        A dict mapping field names to JSON-serializable values.
    """
    return {
        k: (v.isoformat() if isinstance(v, dt.datetime) else v)
        for k, v in items
    }


@dataclass(kw_only=True)
class TestResult:
    """Result of a single test case from a benchmark evaluation.

    Attributes:
        instance_id: Identifier of the benchmark instance that was evaluated.
        patch_type: The patch state under which the test was run.
        agent_name: The agent that produced that patch.
        test_name: Name of the individual test case.
        passed: Whether the test case passed.
    """

    instance_id: str
    patch_type: PatchType
    agent_name: str
    timestamp: dt.datetime
    test_name: str
    passed: bool

    @staticmethod
    def from_dict(obj: dict[str, Any]) -> TestResult:
        """Construct a :class:`TestResult` from a plain dict.

        Args:
            obj: Mapping containing ``instance_id``, ``patch_type``,
                ``agent_name``, ``test_name``, and ``passed`` keys.

        Returns:
            The constructed :class:`TestResult`.

        Raises:
            Exception: If a required key is missing, empty, or has the
                wrong type.
        """
        instance_id = obj.get('instance_id', None)
        if not instance_id:
            raise Exception('instance_id not in dict')
        if not isinstance(instance_id, str):
            raise Exception('instance_id not str')

        patch_type = obj.get('patch_type', None)
        if not patch_type:
            raise Exception('patch_type not in dict')
        if not isinstance(patch_type, str):
            raise Exception('patch_type not str')

        agent_name = obj.get('agent_name', None)
        if not agent_name:
            raise Exception('agent_name not in dict')
        if not isinstance(agent_name, str):
            raise Exception('agent_name not str')

        timestamp = obj.get('timestamp', None)
        if not timestamp:
            raise Exception('timestamp not in dict')
        if not isinstance(timestamp, str):
            raise Exception('timestamp not str')

        test_name = obj.get('test_name', None)
        if not test_name:
            raise Exception('test_name not in dict')
        if not isinstance(test_name, str):
            raise Exception('test_name not str')

        passed = obj.get('passed', None)
        if passed is None:
            raise Exception('passed not in dict')
        if not isinstance(passed, bool):
            raise Exception('passed not bool')

        return TestResult(
            instance_id=instance_id,
            patch_type=PatchType(patch_type),
            agent_name=agent_name,
            timestamp=dt.datetime.fromisoformat(timestamp),
            test_name=test_name,
            passed=passed,
        )

    def to_dict(self, json_safe: bool = False) -> dict[str, Any]:
        """Convert this :class:`TestResult` to a plain dict.

        Args:
            json_safe: If True, convert datetime fields to ISO 8601
                strings so the result is JSON-serializable.

        Returns:
            A dict with the same fields as this :class:`TestResult`.
        """
        if json_safe:
            return asdict(self, dict_factory=factory)
        else:
            return asdict(self)


@dataclass(kw_only=True)
class TestResultsFilename:
    """A parsed/structured representation of a prediction filename.

    Prediction files use filenames of the form
    ``{instance_id}-{patch_type}-{agent_name}-{timestamp}``, where each
    field is escaped to keep the ``-`` field separator unambiguous.

    Attributes:
        instance_id: The SWE-bench instance ID the prediction is for.
        patch_type: The type of patch the prediction represents.
        agent_name: The name of the agent that produced the prediction.
        timestamp: The UTC timestamp of when the prediction was created.
    """

    instance_id: str
    patch_type: PatchType
    agent_name: str
    timestamp: dt.datetime

    @staticmethod
    def _escape(s: str) -> str:
        """Escape a field value so it can be safely embedded in a filename.

        Args:
            s: The raw field value to escape.

        Returns:
            The escaped value, with '%' and '-' replaced with sequences that
            cannot collide with the '-' field separator.
        """
        return s.replace('%', '%25').replace('-', '%2D')

    @staticmethod
    def _unescape(s: str) -> str:
        """Reverse the escaping applied by ``_escape``.

        Args:
            s: The escaped field value, as extracted from a filename.

        Returns:
            The original, unescaped field value.
        """
        return s.replace('%2D', '-').replace('%25', '%')

    def encode(self) -> str:
        """Encode this instance as a prediction filename.

        Returns:
            The filename in the form
            ``{instance_id}-{patch_type}-{agent_name}-{timestamp}``, with
            each field escaped and the timestamp converted to UTC.
        """
        ts = self.timestamp.astimezone(dt.UTC).strftime(_TIMESTAMP_FMT)
        return (
            f'{TestResultsFilename._escape(self.instance_id)}-'
            f'{TestResultsFilename._escape(self.patch_type)}-'
            f'{TestResultsFilename._escape(self.agent_name)}-'
            f'{ts}'
        )

    @staticmethod
    def decode(filename: str) -> TestResultsFilename:
        """Parse a prediction filename into a ``PredFilename`` instance.

        Args:
            filename: The filename to parse, as produced by ``encode``.

        Returns:
            The decoded ``PredFilename``, with the timestamp set to UTC.

        Raises:
            ValueError: If ``filename`` does not match the expected format.
        """
        m = _FILENAME_RE.match(filename)
        if not m:
            raise ValueError(f'Cannot parse filename: {filename!r}')
        ts = dt.datetime.strptime(m['timestamp'], _TIMESTAMP_FMT).replace(
            tzinfo=dt.UTC
        )
        return TestResultsFilename(
            instance_id=TestResultsFilename._unescape(m['instance_id']),
            patch_type=PatchType(
                TestResultsFilename._unescape(m['patch_type'])
            ),
            agent_name=TestResultsFilename._unescape(m['agent_name']),
            timestamp=ts,
        )


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
    timestamp: dt.datetime
    dockerfile_path: Path
    patch_type: PatchType
    agent_name: str
    pred: Pred | None
    image: Image | None
    container: Container | None

    def __init__(
        self,
        instance_id: str,
        timestamp: dt.datetime,
        patch_type: PatchType,
        agent_name: str,
        pred: Pred | None,
    ):
        """Initialize the evaluator for the given instance.

        Args:
            instance_id: Identifier for the benchmark instance. Used to
                locate the Dockerfile under ``DOCKERFILES_BASE`` and to
                name the resulting image and container.
            timestamp: Timestamp of the start of the run.
            patch_type: The patch state this evaluator is running under.
            agent_name: Name of the agent that produced ``pred``.
            pred: The model-generated patch to apply, or ``None`` when
                ``patch_type`` is :attr:`PatchType.BEFORE_PATCH`.
        """
        self.instance_id = instance_id
        self.timestamp = timestamp
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
        """

        client = docker.from_env()
        self.image, _ = client.images.build(
            path=str(self.dockerfile_path.parent.resolve()),
            tag=f'sbmdt-{self.instance_id}:latest',
            labels={LABEL_KEY: LABEL_VALUE},
        )
        self.container = client.containers.run(
            self.image,
            command='/bin/bash',
            name=f'sbmdt-{self.instance_id}',
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
