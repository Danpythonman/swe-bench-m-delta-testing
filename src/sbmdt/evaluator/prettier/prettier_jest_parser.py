"""
Utilities for parsing Jest JSON test results into :class:`TestResult` objects.

Jest's ``--json`` flag produces output with this shape::

    {
        "testResults": [
            {
                "testFilePath": "/testbed/tests/foo.js",
                "testResults": [
                    {
                        "fullName": "describe title test name",
                        "status": "passed" | "failed" | "pending" | "todo"
                    },
                    ...
                ]
            },
            ...
        ]
    }

A test is considered passed when ``status == "passed"``. All other statuses
(``"failed"``, ``"pending"``, ``"todo"``) are treated as not-passed.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sbmdt.evaluator.base import PatchType, TestResult

__all__ = [
    'results_json_to_test_results',
]

log = logging.getLogger(__name__)


def results_json_to_test_results(
    instance_id: str,
    patch_type: PatchType,
    agent_name: str,
    json_string: str,
) -> list[TestResult]:
    """Parse a Jest JSON output string into :class:`TestResult` objects.

    Iterates over all individual test cases nested inside ``testResults``.
    A test is considered passed when its ``status`` field equals ``"passed"``.
    Test cases with no ``fullName`` key are skipped with a warning.

    Args:
        instance_id: Identifier of the benchmark instance that produced the
                     results.
        patch_type: The patch state under which the tests were run.
        agent_name: Name of the agent that produced the patch.
        json_string: Jest ``--json`` output string to parse.

    Returns:
        A list of :class:`TestResult`, one per parseable test case entry.
    """

    data: Any = json.loads(json_string)
    suite_results: list[Any] = data.get('testResults', [])

    results: list[TestResult] = []
    for suite in suite_results:
        test_cases: list[Any] = suite.get('testResults', [])
        for tc in test_cases:
            full_name: str | None = tc.get('fullName')
            if not full_name:
                log.warning('test case missing fullName, skipping')
                continue
            status: str | None = tc.get('status')
            results.append(
                TestResult(
                    instance_id=instance_id,
                    patch_type=patch_type,
                    agent_name=agent_name,
                    test_name=full_name,
                    passed=(status == 'passed'),
                )
            )

    return results