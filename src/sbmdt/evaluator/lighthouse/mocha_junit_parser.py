"""
Utilities for parsing mocha-junit-reporter XML test results into
:class:`TestResult` objects.
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
    """Parse a mocha-junit-reporter XML string into a list of
    :class:`TestResult`.

    mocha-junit-reporter nests ``<testcase>`` elements inside one or more
    ``<testsuite>`` elements under the root ``<testsuites>`` element, so this
    searches recursively rather than only at the top level. A test is
    considered passed if it has no ``<failure>`` child element.

    Args:
        instance_id: Identifier of the benchmark instance that produced the
                     results.
        patch_type: The patch state under which the tests were run.
        xml_string: mocha-junit-reporter-format XML string to parse.

    Returns:
        A list of :class:`TestResult`, one per parseable ``<testcase>``
        element.
    """

    root = ET.fromstring(xml_string)

    results: list[TestResult] = []
    for tc in root.findall('.//testcase'):
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
