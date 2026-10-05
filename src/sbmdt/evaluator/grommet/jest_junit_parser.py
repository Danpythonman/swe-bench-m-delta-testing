"""
Utilities for parsing jest-junit XML test results into :class:`TestResult`
objects.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import xml.etree.ElementTree as ET
from typing import Final

from sbmdt.evaluator.base import PatchType, TestResult
from sbmdt.evaluator.paths import repo_relative

__all__ = [
    'results_xml_to_test_results',
]

log = logging.getLogger(__name__)

JS_TEST_FILE: Final[re.Pattern[str]] = re.compile(r'\.(?:[cm]?jsx?|tsx?)$')


def results_xml_to_test_results(
    instance_id: str,
    patch_type: PatchType,
    agent_name: str,
    xml_string: str,
    timestamp: dt.datetime,
) -> list[TestResult]:
    """Parse a jest-junit XML string into a list of :class:`TestResult`.

    jest-junit nests ``<testcase>`` elements inside one or more
    ``<testsuite>`` elements under the root ``<testsuites>`` element, so
    this searches recursively rather than only at the top level. A test is
    considered passed if it has no ``<failure>`` child element.

    Args:
        instance_id: Identifier of the benchmark instance that produced the
                     results.
        patch_type: The patch state under which the tests were run.
        xml_string: jest-junit-format XML string to parse.

    Returns:
        A list of :class:`TestResult`, one per parseable ``<testcase>``
        element.
    """

    root = ET.fromstring(xml_string)

    results: list[TestResult] = []
    suites = [root] if root.tag == 'testsuite' else root.iter('testsuite')
    for suite in suites:
        # The evaluators ask jest-junit for the file both ways: a ``file``
        # attribute per testcase (newer releases) and the file path as the
        # suite name (JEST_JUNIT_SUITE_NAME='{filepath}', older ones too).
        suite_name = suite.get('name', '')
        suite_file = suite.get('file') or (
            suite_name if JS_TEST_FILE.search(suite_name) else None
        )
        for tc in suite.findall('testcase'):
            test_name = tc.get('name')
            if test_name is None:
                log.warning('no test name')
                continue
            results.append(
                TestResult(
                    instance_id=instance_id,
                    patch_type=patch_type,
                    agent_name=agent_name,
                    timestamp=timestamp,
                    test_name=test_name,
                    passed=(tc.find('failure') is None),
                    test_file=repo_relative(tc.get('file') or suite_file),
                )
            )

    return results
