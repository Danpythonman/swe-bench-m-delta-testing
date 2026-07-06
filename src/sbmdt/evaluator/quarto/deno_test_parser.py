"""
Utilities for parsing Deno's built-in test runner text output into
:class:`TestResult` objects.

The Deno version bundled with the Quarto testbed images (1.22) predates
``deno test``'s structured output flags (``--reporter``/``--junit``), so
results are scraped from its plain-text summary lines instead of a
machine-readable format.
"""

from __future__ import annotations

import logging
import re

from sbmdt.evaluator.base import PatchType, TestResult

__all__ = [
    'results_text_to_test_results',
]

log = logging.getLogger(__name__)

# Matches lines like:
#   [unit] > path - removeIfExists ... ok (174ms)
#   [smoke] > quarto render foo.qmd ... FAILED (2ms)
# Deno only emits "ignored" for tests skipped via `.ignore`/`t.step`, which
# have no pass/fail outcome, so they are excluded from the parsed results
# rather than being reported as one or the other.
TEST_LINE: re.Pattern[str] = re.compile(
    r'^(?P<name>.+) \.\.\. (?P<status>ok|FAILED|ignored)\b', re.MULTILINE
)


def results_text_to_test_results(
    instance_id: str, patch_type: PatchType, agent_name: str, output: str
) -> list[TestResult]:
    """Parse ``deno test``'s plain-text output into a list of
    :class:`TestResult`.

    Args:
        instance_id: Identifier of the benchmark instance that produced the
                     results.
        patch_type: The patch state under which the tests were run.
        agent_name: Name of the agent that produced the evaluated patch.
        output: Captured stdout/stderr of a ``deno test`` invocation run
            with ``NO_COLOR=1`` (so lines are free of ANSI escape codes).

    Returns:
        A list of :class:`TestResult`, one per parsed test line whose
        status was ``ok`` or ``FAILED`` (``ignored`` tests are skipped).
    """

    results: list[TestResult] = []
    for match in TEST_LINE.finditer(output):
        status = match.group('status')
        if status == 'ignored':
            continue
        results.append(
            TestResult(
                instance_id=instance_id,
                patch_type=patch_type,
                agent_name=agent_name,
                test_name=match.group('name'),
                passed=(status == 'ok'),
            )
        )

    return results
