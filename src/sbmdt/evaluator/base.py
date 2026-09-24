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
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, final

import docker
from docker.models.containers import Container
from docker.models.images import Image

from sbmdt.env import DOCKERFILES_BASE
from sbmdt.patches import (
    drop_mode_only_sections,
    drop_unappliable_binary,
    split_diff,
    split_diff_by_file,
    test_assets_for,
    test_patch_for,
)
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
TEST_PATCH_FILE: Final[str] = '/tmp/test.patch'

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


MODEL_PATCH_TYPES: Final[frozenset[str]] = frozenset(
    {PatchType.WITH_IMAGE, PatchType.WITHOUT_IMAGE}
)
"""Patch types the gold test patch may be applied on top of."""

# A small number of legacy prebuilt images were created from a checkout that
# does not match the original GitHub PR base used by their gold patch. These
# overrides restore the verified PR base before applying benchmark patches.
PATCH_BASE_COMMIT_OVERRIDES: Final[dict[str, str]] = {
    'PrismJS__prism-1585': '11695629f12925c586702453beaee5f4825d0ebd',
    'PrismJS__prism-1602': 'da474c77e2da4103192cd29827d3c0c64f9b8801',
    'PrismJS__prism-1895': 'f0a10669acd07ddcd88eccef2675f488068c98e9',
    'PrismJS__prism-2195': '0bf73dc7813cdd1eadb15730d8c9f2d00f208df8',
    'PrismJS__prism-2703': '01af04ed2be7cf18e02997428d3cc19addfc6012',
    'PrismJS__prism-2861': 'e0ee93f138b7da294a28db50b97c22977fdfc8ed',
    'carbon-design-system__carbon-12410': (
        '41e692d24732a34c57861c6023536db1b74d548c'
    ),
    'bpmn-io__bpmn-js-1083': 'd0ff81a6e7dfa10138e773820fd2970fb22140db',
    'bpmn-io__bpmn-js-1578': '143603a26dbcc6dec8ca37df94562fc9050e96b4',
    'bpmn-io__bpmn-js-1584': '7baefd7bc33b2c0e2caf61322e7e950d10f737fe',
    'bpmn-io__bpmn-js-1655': '7478388070d83e8802c873e8480dbf23ae3ace3a',
    'openlayers__openlayers-11047': 'bfc035415edabfe297faf6d68575d8118e088fba',
}

# For reference runs, checking out the immutable official PR head is more
# faithful than replaying an old serialized diff through legacy Git versions.
GOLD_COMMIT_OVERRIDES: Final[dict[str, str]] = {
    'PrismJS__prism-1585': '84f12f1e304de7cb6b72b7325506d4d47b1a9075',
    'PrismJS__prism-1602': '75a0d1787df143523f5bb4abf0da867321af3b33',
    'PrismJS__prism-1895': '0dc2101940e0c9d97226b71fdec5539ff9095965',
    'PrismJS__prism-2195': 'db4af6cd0909e191dc7a582f12b123e6dbf197e5',
    'PrismJS__prism-2703': '32251f3d81521867ecef9f4d696403e6e6aeafe5',
    'PrismJS__prism-2861': 'b1103ebf4a0a7a7f38f25c36d7d8c653ef02c2c5',
    'bpmn-io__bpmn-js-1083': 'de1e1be92b5319ad989481565044596536fd3ca4',
    'bpmn-io__bpmn-js-1578': 'a09636f773fe425b204919f216850c58ab44bd03',
    'bpmn-io__bpmn-js-1584': 'f7b846dfe50613cad265d452f54e509b86927dff',
    'bpmn-io__bpmn-js-1655': 'bd2166a5731bbd813ffd247e9ca33ea194f17304',
    'carbon-design-system__carbon-12410': (
        'fe45ba2faf416bad55fb2495a2bb4aebe8411d78'
    ),
    'openlayers__openlayers-13212': (
        '75f66757ef6a46be50513c40a3cc022026d8e5c2'
    ),
}


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


# Docker Hub throttles anonymous (unauthenticated) image pulls per source
# IP over a rolling window. Every EC2 worker in a batch shares the same
# NAT gateway, so pulling the base image from many workers at once can
# exhaust that shared quota well before any one worker is individually
# abusive -- this showed up as unrelated instances across several
# different repositories all failing within the same few minutes, which
# first looked like environment or resource contention until the actual
# docker.errors.BuildError text turned out to name the real cause. The
# quota resets on a sliding window, so a short wait and retry recovers
# once the burst that exhausted it has passed, without needing Docker Hub
# credentials this project does not have.
_IMAGE_BUILD_RETRIES: Final[int] = 4
_IMAGE_BUILD_RETRY_DELAY_SECONDS: Final[float] = 45.0


def _build_image_with_retry(
    client: docker.DockerClient, **build_kwargs: Any
) -> tuple[Image, Any]:
    """Build a Docker image, retrying on a Docker Hub rate limit.

    Every other :class:`docker.errors.BuildError` (a real Dockerfile
    problem, a missing file, etc.) is not retried and raises immediately,
    since retrying it would only waste the same amount of time again for
    the same guaranteed failure.

    Args:
        client: Docker client to build with.
        **build_kwargs: Forwarded to :meth:`docker.models.images.build`.

    Returns:
        Whatever :meth:`docker.models.images.build` returns.

    Raises:
        docker.errors.BuildError: If every retry is also rate-limited, or
            immediately for any other build failure.
    """
    for attempt in range(1, _IMAGE_BUILD_RETRIES + 1):
        try:
            return client.images.build(**build_kwargs)
        except docker.errors.BuildError as e:
            rate_limited = 'toomanyrequests' in str(e)
            if not rate_limited or attempt == _IMAGE_BUILD_RETRIES:
                raise
            log.info(
                f'Docker Hub rate limit hit on image build (attempt '
                f'{attempt}/{_IMAGE_BUILD_RETRIES}), retrying in '
                f'{_IMAGE_BUILD_RETRY_DELAY_SECONDS}s...'
            )
            time.sleep(_IMAGE_BUILD_RETRY_DELAY_SECONDS)
    raise Exception('image build retries exceeded')


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
        apply_test_patch: bool = False,
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
            apply_test_patch: Whether to apply the instance's
                ``test_patch.diff`` on top of ``pred`` (see
                :meth:`apply_test_patch`). Off by default so existing
                behaviour is unchanged.
        """
        self.instance_id = instance_id
        self.timestamp = timestamp
        self.dockerfile_path = DOCKERFILES_BASE / instance_id / 'Dockerfile'
        self.patch_type = patch_type
        self.agent_name = agent_name
        self.pred = pred
        self.apply_test_patch_enabled = apply_test_patch
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
        # Note that rm=True remove intermediate containers after build
        log.info('Building image...')
        self.image, _ = _build_image_with_retry(
            client,
            path=str(self.dockerfile_path.parent.resolve()),
            tag=f'{resource_name}:latest',
            labels={LABEL_KEY: LABEL_VALUE},
            rm=True,
            forcerm=True,
        )
        log.info('Running container...')
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

        # A unified diff must terminate its last line. JSON predictions can
        # omit that transport newline, which git reports as a corrupt patch.
        # Preserve every patch line, including explicit no-newline markers.
        patch = self.pred.model_patch
        if patch and not patch.endswith('\n'):
            patch += '\n'
        patch, dropped = drop_unappliable_binary(patch)
        if dropped:
            log.info(
                f'{self.instance_id}: dropped {len(dropped)} binary '
                f'section(s) without embedded data: {dropped}'
            )
        patch, dropped_modes = drop_mode_only_sections(patch)
        if dropped_modes:
            log.info(
                f'{self.instance_id}: dropped {len(dropped_modes)} pure '
                f'file-mode section(s): {dropped_modes}'
            )
        is_gold = str(getattr(self, 'patch_type', '')) == 'gold'
        sections = split_diff(patch) if is_gold else (patch,)
        gold_test_section = sections[1] if is_gold else ''
        overrides = globals().get('PATCH_BASE_COMMIT_OVERRIDES', {})
        # The released gold predictions lost their CR bytes in transport,
        # so a test file committed with CRLF comes back LF-only when it is
        # rebuilt from one (the strip below). Most suites never notice.
        # Prism's do: an expected token can hold a literal CRLF, as
        # prism-1500's multi-line SQL string does, and the gold patch then
        # fails its own test in every run. When the instance's test patch
        # is that same diff with its CRs intact, apply it instead, to the
        # working tree, exactly as a base run does -- so base and gold also
        # test against byte-identical files.
        exact_test = (
            test_patch_for(self.instance_id)
            if is_gold and gold_test_section
            and self.instance_id not in overrides
            else ''
        )
        if '\r' in exact_test and exact_test.replace(
            '\r', ''
        ) == gold_test_section.replace('\r', ''):
            log.info(
                f'{self.instance_id}: gold test files keep the CRLF bytes '
                f'of the test patch'
            )
            sections = (sections[0], exact_test)
            gold_test_section = exact_test
        else:
            exact_test = ''
        for section_number, section in enumerate(
            filter(None, sections), start=1
        ):
            materialize_exact_blobs = (
                is_gold and section == gold_test_section and not exact_test
            ) or self.instance_id in overrides
            if materialize_exact_blobs:
                # Materialize tracked files directly from Git's blob store.
                # Legacy images may have checkout filters or line-ending
                # conversion that make working-tree bytes differ even when
                # HEAD has the exact old blobs named by the submitted diff.
                paths = re.findall(r'^diff --git a/\S+ b/(\S+)', section, re.M)
                for path in paths:
                    _, restore_output = self.container.exec_run(
                        [
                            'bash',
                            '-c',
                            'if git cat-file -e "HEAD:$1" 2>/dev/null; '
                            'then git cat-file blob "HEAD:$1" > "$1"; '
                            'sed -i \'s/\\r$//\' "$1"; '
                            'else rm -rf -- "$1"; fi',
                            'git-materialize-gold-test',
                            path,
                        ],
                        workdir='/testbed',
                        stream=False,
                    )
                    assert isinstance(restore_output, bytes)
            exit_code, outputs = self.apply_diff_text(section)
            if exit_code != 0:
                # git apply is all-or-nothing, so one section it cannot
                # place throws away the whole submission. Across this
                # project's runs that cost 234 evaluations over 192
                # instances, and the file named in the error was almost
                # always package-lock.json, package.json or yarn.lock -
                # manifests the image had already regenerated with its own
                # npm install, so the model's diff of them could never
                # match. Retrying file by file keeps the code change that
                # the suite actually measures instead of discarding it
                # along with the lockfile hunk.
                #
                # What could not be placed is named in the log rather than
                # swallowed: a run that dropped a section is not the same
                # as a clean one, and the reader has to be able to tell.
                per_file = split_diff_by_file(section)
                if len(per_file) > 1:
                    applied: list[str] = []
                    refused: list[str] = []
                    for path, chunk in per_file:
                        chunk_code, chunk_outputs = self.apply_diff_text(
                            chunk
                        )
                        if chunk_code == 0:
                            applied.append(path)
                        else:
                            refused.append(path)
                            outputs.extend(chunk_outputs)
                    if applied:
                        log.warning(
                            '%s: applied %d of %d file(s) in patch section '
                            '%s; refused %s',
                            self.instance_id, len(applied), len(per_file),
                            section_number, refused,
                        )
                        continue
            if exit_code != 0:
                log.error('Failed to apply patch section %s', section_number)
                raise Exception(
                    f'Failed to apply patch for {self.instance_id}: '
                    f'{outputs[-1]}'
                )

    def apply_diff_text(self, section: str) -> tuple[int, list[str]]:
        """Try hard to apply one diff to /testbed, and say what happened.

        Walks the ``git apply`` ladder (plain, ``--recount``, ``--3way``)
        and, when all three refuse, falls back to ``patch --forward``,
        which matches on context and so does not need the pre-image blob
        that ``--3way`` demands.

        Args:
            section: The unified diff text to apply.

        Returns:
            ``(exit_code, outputs)``, where ``exit_code`` is 0 only if the
            diff landed and ``outputs`` holds every command's output in
            order, for the error message.
        """
        assert self.container is not None
        write_to_container(self.container, PATCH_FILE, section)
        outputs: list[str] = []
        attempts = (
            (f'git apply --check {PATCH_FILE}', f'git apply {PATCH_FILE}'),
            (
                f'git apply --check --recount {PATCH_FILE}',
                f'git apply --recount {PATCH_FILE}',
            ),
            (
                'git apply --check --3way --whitespace=nowarn '
                f'{PATCH_FILE}',
                f'git apply --3way --whitespace=nowarn {PATCH_FILE}',
            ),
        )
        exit_code = 1
        for check_command, apply_command in attempts:
            check_code, check_output = self.container.exec_run(
                check_command, workdir='/testbed', stream=False
            )
            assert isinstance(check_output, bytes)
            outputs.append(check_output.decode())
            if check_code != 0:
                continue
            exit_code, output = self.container.exec_run(
                apply_command, workdir='/testbed', stream=False
            )
            assert isinstance(output, bytes)
            outputs.append(output.decode())
            log.info(exit_code)
            log.info(output.decode())
            if exit_code == 0:
                break
        # Last rung, for every instance rather than only the ones on the
        # override list. `git apply --3way` needs the diff's pre-image
        # blob to be in the object store, and for a test patch written
        # against a commit the image does not contain it is not: git
        # refuses with "does not match index" and the whole instance is
        # lost with no result at all. GNU patch does not need the blob --
        # it matches on context -- so it lands these cleanly. Seven tasks
        # in the 2026-09-21 round died here (openlayers-13155, -13226,
        # -11088, -14659 and three bpmn-js), none of them on the override
        # list, which is why the gate had to go rather than grow.
        #
        # Widening it is safe because this only runs once all three git
        # attempts have already failed, and --dry-run still has to pass
        # first, so a patch that does not really fit is still refused.
        if exit_code != 0:
            dry_code, dry_output = self.container.exec_run(
                f'patch --dry-run --batch --forward -p1 -i {PATCH_FILE}',
                workdir='/testbed',
                stream=False,
            )
            assert isinstance(dry_output, bytes)
            outputs.append(dry_output.decode())
            if dry_code == 0:
                exit_code, output = self.container.exec_run(
                    f'patch --batch --forward -p1 -i {PATCH_FILE}',
                    workdir='/testbed',
                    stream=False,
                )
                assert isinstance(output, bytes)
                outputs.append(output.decode())
        return exit_code, outputs

    def restore_patch_base(self) -> None:
        """Check out a verified PR base when a legacy image is mismatched."""

        target_commit = (
            GOLD_COMMIT_OVERRIDES.get(self.instance_id)
            if self.patch_type == PatchType.GOLD
            else PATCH_BASE_COMMIT_OVERRIDES.get(self.instance_id)
        )
        if target_commit is None:
            return
        if self.container is None:
            raise Exception('no container')
        log.info(
            'Restoring verified patch base %s for %s',
            target_commit,
            self.instance_id,
        )
        owner, _, repository_and_pr = self.instance_id.partition('__')
        repository, _, _ = repository_and_pr.rpartition('-')
        if not owner or not repository:
            raise ValueError(f'Invalid instance ID: {self.instance_id}')
        repository_url = f'https://github.com/{owner}/{repository}.git'
        checkout_command = (
            # Legacy images can have core.autocrlf enabled. That leaves the
            # working tree different from the exact blobs named by a patch,
            # even after checking out the correct commit.
            'git config core.autocrlf false && '
            f'git fetch --depth=1 {repository_url} {target_commit} && '
            'git checkout --detach --force FETCH_HEAD && '
            'git reset --hard FETCH_HEAD'
        )
        exit_code, output = self.container.exec_run(
            ['bash', '-lc', checkout_command],
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)
        log.info(exit_code)
        log.info(output.decode())
        if exit_code != 0:
            raise Exception(
                'Failed to restore verified patch base for '
                f'{self.instance_id}: '
                f'{output.decode()}'
            )

    def apply_test_patch(self) -> None:
        """Apply the instance's ``test_patch.diff`` on top of the model patch.

        SWE-bench scores a model patch against the maintainer's tests, so
        those tests have to be present in the container no matter what the
        model wrote. This project's ``gold_patch.diff`` bundles the code
        fix and the new tests together, so a model run — which only ever
        receives ``pred.model_patch`` — never sees them, and its
        FAIL_TO_PASS tests are absent rather than failing.

        Applying the test half separately fixes that. The test files are
        first restored to their committed state, because a model patch may
        have edited the same files and would otherwise make the test patch
        conflict.

        The test half is derived from ``gold_patch.diff`` when no
        ``test_patch.diff`` has been written, so this works on a fresh
        clone without ``scripts/split_gold_patch.py`` having been run.

        Does nothing when the gold patch touches no test file.

        Raises:
            Exception: If the container has not been started, or if
                ``git apply`` exits non-zero.
            FileNotFoundError: If the instance has no patch to derive the
                test half from.
        """

        if self.container is None:
            raise Exception('no container')

        test_patch = test_patch_for(self.instance_id)
        if not test_patch.strip():
            log.info('Test patch is empty, nothing to apply')
            self._restore_test_assets()
            return

        # Discard any model edits to the files the test patch touches, so
        # the patch applies against the state it was generated from. Paths
        # come from the post-image (b/) side of each diff header.
        #
        # Restore each path that exists in HEAD individually. A single
        # `git checkout -- a b c` where b/c are newly-added test files
        # returns non-zero; on some git builds that mixed failure mode has
        # been observed not to reliably restore the tracked paths that
        # *do* exist (openlayers-14066: model edited GeoTIFF.test.js, the
        # bundled checkout reported only missing rendering fixtures, then
        # the test patch failed against the still-dirty test file).
        paths = re.findall(r'^diff --git a/\S+ b/(\S+)', test_patch, re.M)
        for path in paths:
            exit_code, output = self.container.exec_run(
                [
                    'bash',
                    '-c',
                    'git cat-file -e "HEAD:$1" 2>/dev/null '
                    '&& git checkout HEAD -- "$1"',
                    'git-restore-test-path',
                    path,
                ],
                workdir='/testbed',
                stream=False,
            )
            assert isinstance(output, bytes)
            # Newly added test files are absent from HEAD, so cat-file
            # fails and we skip them; the patch creates them.
            if exit_code != 0 and output.strip():
                log.info(
                    f'git checkout of test path {path!r} returned '
                    f'{exit_code} (expected for newly added files): '
                    f'{output.decode()}'
                )

            # If the model (or a prior partial apply) left an untracked
            # copy of a path the test patch wants to *add*, git apply
            # fails with "already exists in working directory"
            # (bpmn-js-1382, carbon-8720). Remove only paths that are
            # not in HEAD so we do not clobber restored tracked files.
            exit_code, output = self.container.exec_run(
                [
                    'bash',
                    '-c',
                    'if ! git cat-file -e "HEAD:$1" 2>/dev/null '
                    '&& [ -e "$1" ]; then rm -rf -- "$1"; fi',
                    'git-clear-untracked-test-path',
                    path,
                ],
                workdir='/testbed',
                stream=False,
            )
            assert isinstance(output, bytes)
            if exit_code != 0 and output.strip():
                log.info(
                    f'clearing untracked test path {path!r} returned '
                    f'{exit_code}: {output.decode()}'
                )

        write_to_container(self.container, TEST_PATCH_FILE, test_patch)

        outputs = []
        commands = (
            f'git apply {TEST_PATCH_FILE}',
            f'git apply --recount {TEST_PATCH_FILE}',
            f'git apply --3way --whitespace=nowarn {TEST_PATCH_FILE}',
        )
        for index, command in enumerate(commands):
            exit_code, output = self.container.exec_run(
                command,
                workdir='/testbed',
                stream=False,
            )
            assert isinstance(output, bytes)
            outputs.append(output.decode())
            log.info(exit_code)
            log.info(output.decode())
            if exit_code == 0:
                break
            if index < len(commands) - 1:
                log.info(
                    'Test-patch apply failed; trying the next safe fallback'
                )

        if exit_code != 0:
            log.error('Failed to apply test patch')
            raise Exception(
                f'Failed to apply test patch for {self.instance_id}: '
                f'{outputs[-1]}'
            )

        self._restore_test_assets()

    def _restore_test_assets(self) -> None:
        """Write the binary files the test patch names but cannot carry.

        ``test_patch_for`` drops ``Binary files ... differ`` stubs because
        ``git apply`` has no bytes to write. For openlayers those are the
        ``expected.png`` images its rendering tests compare against, so
        without this step a new rendering case has no oracle and an
        updated one compares against the stale image. The bytes come
        from ``scripts/fetch_test_assets.py`` and are checked against the
        stub's blob id before use (``patches.test_assets_for``).
        """
        assert self.container is not None
        assets = test_assets_for(self.instance_id)
        for path, data in assets:
            target = f'/testbed/{path}'
            if data is None:
                self.container.exec_run(['rm', '-f', target])
                continue
            self.container.exec_run(
                ['mkdir', '-p', target.rsplit('/', 1)[0]]
            )
            write_to_container(self.container, target, data)
        if assets:
            log.info(
                f'{self.instance_id}: restored {len(assets)} binary test '
                f'file(s) the test patch names but does not carry'
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

        Stages: :meth:`provision` (build image, start container), then apply
        the requested patch and benchmark test patch, then :meth:`setup`,
        :meth:`evaluate`, and :meth:`cleanup`. Patches intentionally precede
        setup because dependency installation can rewrite tracked manifests
        and lockfiles, which would make otherwise valid diffs fail to apply.
        :meth:`cleanup` runs even if an earlier stage raises, so a failed run
        never leaves behind a container/image that has to be removed manually
        before retrying.
        """
        try:
            log.info('Provisioning...')
            self.provision()
            if hasattr(self, 'restore_patch_base'):
                self.restore_patch_base()
            gold_is_checked_out = (
                self.patch_type == PatchType.GOLD
                and self.instance_id
                in globals().get('GOLD_COMMIT_OVERRIDES', {})
            )
            if (
                self.patch_type != PatchType.BEFORE_PATCH
                and not gold_is_checked_out
            ):
                log.info('Applying patch...')
                self.apply_patch()
            elif gold_is_checked_out:
                log.info('Official gold commit already checked out')
            else:
                log.info('No patch to apply')
            if self.apply_test_patch_enabled:
                if (
                    self.patch_type in MODEL_PATCH_TYPES
                    or self.patch_type == PatchType.BEFORE_PATCH
                ):
                    log.info('Applying test patch...')
                    self.apply_test_patch()
                else:
                    # Gold already carries the maintainer's tests in the
                    # same diff. The baseline needs the separate test patch
                    # so new regression tests can form FAIL_TO_PASS.
                    log.info(f'Not applying test patch for {self.patch_type}')
            log.info('Setting up...')
            self.setup()
            log.info('Evaluating...')
            results = self.evaluate()
        except Exception:
            # Without this the stage that failed is invisible: the run
            # goes straight from its last INFO line to "Cleaning up",
            # leaving no cause to diagnose.
            log.exception(
                f'Run failed for {self.instance_id} ({self.patch_type})'
            )
            raise
        finally:
            log.info('Cleaning up...')
            self.cleanup()
        log.info('Returning results...')
        return results
