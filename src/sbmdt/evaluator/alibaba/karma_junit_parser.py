"""
Utilities for parsing JUnit XML test results into :class:`TestResult` objects.
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
    """Parse a JUnit XML string into a list of :class:`TestResult` objects.

    Iterates over all ``<testcase>`` elements in the XML. A test is
    considered passed if it has no ``<failure>`` child element.
    Test cases with no ``name`` attribute are skipped with a warning.

    Args:
        instance_id: Identifier of the benchmark instance that produced the
                     results.
        patch_type: The patch state under which the tests were run.
        xml_string: JUnit-format XML string to parse.

    Returns:
        A list of :class:`TestResult`, one per parseable ``<testcase>``
        element.
    """

    root = ET.fromstring(xml_string)

    results: list[TestResult] = []
    for tc in root.findall('testcase'):
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
            )
        )

    return results
