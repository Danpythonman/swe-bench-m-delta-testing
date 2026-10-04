"""Shared base for SWE-bench Verified (Python) evaluators.

Every Verified image ships the repository at ``/testbed`` with a conda
environment named ``testbed`` that already has the project installed, so
unlike the SWE-bench M evaluators there is nothing to install. What
differs per repository is how its tests are selected and launched, which
subclasses supply through :meth:`PythonEvaluator.test_command`.

Results are not scraped from console output. Instead a small module of
this project's own (see ``injected/``) is copied into the container and
hooks the test runner, writing one JSON line per test outcome to
:data:`RESULTS_FILE`. :func:`results_to_test_results` folds those lines
into one :class:`~sbmdt.evaluator.base.TestResult` per test.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from abc import abstractmethod
from pathlib import Path
from typing import Final, override

from sbmdt.evaluator.base import Evaluator, PatchType, TestResult
from sbmdt.evaluator.python.selection import changed_paths
from sbmdt.patches import test_patch_for
from sbmdt.utils import read_from_container, write_to_container

__all__ = [
    'INJECTED_DIR',
    'PythonEvaluator',
    'RESULTS_FILE',
    'results_to_test_results',
]

log = logging.getLogger(__name__)

# Where this project's injected modules are copied inside the container.
INJECTED_DIR: Final[str] = '/tmp/sbmdt'

# JSON-lines file the injected modules append test outcomes to.
RESULTS_FILE: Final[str] = '/tmp/sbmdt-results.jsonl'

_INJECTED_SOURCES: Final[Path] = Path(__file__).parent / 'injected'

_ACTIVATE: Final[str] = 'source /opt/miniconda3/bin/activate testbed'

# A hung test (a deadlocked subprocess, a network call with no timeout)
# would otherwise block the run indefinitely. Results are written as they
# happen, so whatever finished before the deadline is still collected.
# --kill-after escalates to SIGKILL if the process tree ignores SIGTERM.
TEST_TIMEOUT_SECONDS: Final[int] = 1800
_TIMEOUT: Final[str] = f'timeout --kill-after=30 {TEST_TIMEOUT_SECONDS}'

# Outcomes the injected modules write, and whether each one is a pass.
# An expected failure counts as a pass: the test behaved as declared.
# Skips carry no verdict and are dropped. A collection error is a failure
# of the module itself, recorded under the module's own name.
_PASSING: Final[frozenset[str]] = frozenset({'passed', 'xfailed'})
_FAILING: Final[frozenset[str]] = frozenset({'failed', 'collect_error'})
_NO_VERDICT: Final[frozenset[str]] = frozenset({'skipped'})


def results_to_test_results(
    jsonl: str,
    instance_id: str,
    patch_type: PatchType,
    agent_name: str,
    timestamp: dt.datetime,
) -> list[TestResult]:
    """Fold injected-module JSON lines into one result per test.

    A test can produce several lines: pytest reports setup, call and
    teardown separately, and a unittest test with failing subtests reports
    each failure. A test fails if any of its lines is a failure, passes if
    none is and at least one is a pass, and is dropped if it was only ever
    skipped. Tests keep the order in which they first appeared.

    Args:
        jsonl: Contents of :data:`RESULTS_FILE`.
        instance_id: Instance the run belongs to.
        patch_type: Patch state the run was made under.
        agent_name: Agent that produced the patch.
        timestamp: Start of the run.

    Returns:
        One :class:`TestResult` per test that reached a verdict.

    Raises:
        ValueError: If a line is malformed or names an unknown outcome.
    """
    verdicts: dict[str, bool | None] = {}
    for line_number, line in enumerate(jsonl.splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        test = record.get('test')
        outcome = record.get('outcome')
        if not isinstance(test, str) or not isinstance(outcome, str):
            raise ValueError(f'line {line_number}: malformed record {line!r}')
        previous = verdicts.get(test)
        if outcome in _FAILING:
            verdicts[test] = False
        elif outcome in _PASSING:
            verdicts[test] = previous if previous is False else True
        elif outcome in _NO_VERDICT:
            verdicts.setdefault(test, None)
        else:
            raise ValueError(
                f'line {line_number}: unknown outcome {outcome!r}'
            )

    return [
        TestResult(
            instance_id=instance_id,
            patch_type=patch_type,
            agent_name=agent_name,
            timestamp=timestamp,
            test_name=test,
            passed=passed,
        )
        for test, passed in verdicts.items()
        if passed is not None
    ]


class PythonEvaluator(Evaluator):
    """Base evaluator for SWE-bench Verified instances.

    Subclasses implement :meth:`test_command`, turning the paths the
    instance's test patch changes into a runner invocation that writes
    :data:`RESULTS_FILE` through one of the injected modules.
    """

    def _exec(
        self, cmd: str, env: dict[str, str] | None = None
    ) -> tuple[int, str]:
        """Run ``cmd`` in ``/testbed`` with the ``testbed`` env active.

        Args:
            cmd: Shell command to run.
            env: Extra environment variables.

        Returns:
            The exit code and the combined stdout/stderr.

        Raises:
            RuntimeError: If the container has not been started.
        """
        if self.container is None:
            raise RuntimeError('Container not initialized')
        exit_code, output = self.container.exec_run(
            ['/bin/bash', '-c', f'{_ACTIVATE} && {cmd}'],
            workdir='/testbed',
            environment=env,
            stream=False,
        )
        assert isinstance(output, bytes)
        assert exit_code is not None
        return exit_code, output.decode('utf-8', errors='replace')

    @abstractmethod
    def test_command(self, paths: list[str]) -> str | None:
        """Build the command that runs the selected tests.

        The command runs in ``/testbed`` with the ``testbed`` environment
        active, :data:`INJECTED_DIR` on ``PYTHONPATH`` and
        ``SBMDT_RESULTS_FILE`` set.

        Args:
            paths: Files the instance's test patch adds or modifies.

        Returns:
            The command, or ``None`` when none of ``paths`` maps to a
            runnable test.
        """
        ...

    @override
    def setup(self) -> None:
        """Copy the injected modules into the container.

        Raises:
            RuntimeError: If the container has not been started.
        """
        if self.container is None:
            raise RuntimeError('Container not initialized')
        # put_archive extracts into an existing directory only.
        self._exec(f'mkdir -p {INJECTED_DIR}')
        for source in sorted(_INJECTED_SOURCES.glob('*.py')):
            write_to_container(
                self.container,
                f'{INJECTED_DIR}/{source.name}',
                source.read_text(),
            )
        log.info(f'Injected modules copied to {INJECTED_DIR}')

    @override
    def evaluate(self) -> list[TestResult]:
        """Run the tests the test patch touches and collect their results.

        Returns:
            One :class:`TestResult` per test that reached a verdict, or an
            empty list when no test was selected or none ran.

        Raises:
            RuntimeError: If the container has not been started.
        """
        if self.container is None:
            raise RuntimeError('Container not initialized')

        paths = changed_paths(test_patch_for(self.instance_id))
        command = self.test_command(paths)
        if command is None:
            log.error(f'No runnable tests among test patch paths {paths}')
            return []

        self._exec(f'rm -f {RESULTS_FILE}')
        log.info(f'Running: {command}')
        exit_code, output = self._exec(
            f'{_TIMEOUT} {command}',
            env={
                'PYTHONPATH': INJECTED_DIR,
                'SBMDT_RESULTS_FILE': RESULTS_FILE,
            },
        )
        # Failing tests make the runner exit non-zero, so that alone is
        # not an error; 124 is timeout(1) giving up on it.
        log.info(f'Test command exit code: {exit_code}')
        if exit_code == 124:
            log.warning(
                f'Tests hit the {TEST_TIMEOUT_SECONDS}s timeout; results '
                'are partial'
            )
        log.info(output)

        try:
            jsonl = read_from_container(self.container, RESULTS_FILE)
        except Exception as exc:
            log.error(f'No results file (no test ran?): {exc}')
            return []
        return results_to_test_results(
            jsonl,
            self.instance_id,
            self.patch_type,
            self.agent_name,
            self.timestamp,
        )

    @override
    def pre_cleanup(self) -> None:
        """Pre-cleanup hook. No-op for this evaluator."""
        pass

    @override
    def post_cleanup(self) -> None:
        """Post-cleanup hook. No-op for this evaluator."""
        pass
