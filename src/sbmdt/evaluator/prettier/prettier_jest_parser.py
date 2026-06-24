"""
Utilities for parsing Jest JSON test results into :class:`TestResult` objects.

Jest's ``--json`` flag produces output with this shape::

    {
        "testResults": [
            {
                "testFilePath": "/testbed/tests/foo.js",
                "assertionResults": [
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

Note: individual test cases are under ``assertionResults``, not ``testResults``.
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
    data: Any = json.loads(json_string)
    suite_results: list[Any] = data.get('testResults', [])

    results: list[TestResult] = []
    for suite in suite_results:
        test_cases: list[Any] = suite.get('assertionResults', [])
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
