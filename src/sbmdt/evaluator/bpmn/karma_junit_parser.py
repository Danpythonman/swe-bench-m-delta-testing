"""
Utilities for parsing Karma JUnit XML test results into :class:`TestResult`
objects for bpmn-js instances.
"""

from __future__ import annotations

import datetime as dt
import logging
import xml.etree.ElementTree as ET

from sbmdt.evaluator.base import PatchType, TestResult

__all__ = [
    'results_xml_to_test_results',
]

log = logging.getLogger(__name__)


def results_xml_to_test_results(
    instance_id: str,
    patch_type: PatchType,
    agent_name: str,
    xml_string: str,
    timestamp: dt.datetime,
) -> list[TestResult]:
    """Parse a Karma JUnit XML string into a list of :class:`TestResult`.

    Karma's ``karma-junit-reporter`` may emit ``<testcase>`` elements either
    directly under the root ``<testsuites>`` element or nested inside one or
    more ``<testsuite>`` children, depending on the reporter version and
    configuration.  This function searches recursively so it works in both
    layouts.  A test is considered passed if it has no ``<failure>`` or
    ``<error>`` child element.

    Args:
        instance_id: Identifier of the benchmark instance that produced the
                     results.
        patch_type: The patch state under which the tests were run.
        agent_name: Name of the agent that produced the patch.
        xml_string: JUnit-format XML string to parse.

    Returns:
        A list of :class:`TestResult`, one per parseable ``<testcase>``
        element.
    """

    root = ET.fromstring(xml_string)

    results: list[TestResult] = []
    # Use .//.  to find <testcase> elements at any depth (handles both flat
    # karma-junit-reporter output and nested <testsuite> wrappers).
    for tc in root.findall('.//testcase'):
        test_name = tc.get('name')
        classname = tc.get('classname', '')

        if test_name is None:
            log.warning('testcase element missing name attribute — skipping')
            continue

        # Build a fully-qualified name so results are unambiguous across
        # different test suites within the same run.
        if classname:
            full_name = f'{classname} {test_name}'
        else:
            full_name = test_name

        passed = tc.find('failure') is None and tc.find('error') is None

        results.append(
            TestResult(
                instance_id=instance_id,
                patch_type=patch_type,
                agent_name=agent_name,
                timestamp=timestamp,
                test_name=full_name,
                passed=passed,
            )
        )

    return results
