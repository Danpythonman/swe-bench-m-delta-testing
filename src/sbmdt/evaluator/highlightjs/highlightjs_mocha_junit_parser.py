"""
Utilities for parsing Mocha JUnit XML results into :class:`TestResult` objects.

``mocha-junit-reporter`` produces output with this shape::

    <testsuites>
        <testsuite name="Suite name" tests="1" failures="0">
            <testcase name="test name" classname="Suite name" time="0.001">
                <!-- present only on failure: -->
                <failure message="AssertionError">...</failure>
            </testcase>
        </testsuite>
    </testsuites>

Because ``testcase`` elements are nested two levels deep (under
``testsuite``), this parser uses the recursive XPath expression
``'.//testcase'`` rather than the direct-child ``'testcase'`` used by
the Karma JUnit parser.

A test is considered passed if it has neither a ``<failure>`` nor an
``<error>`` child element.
"""

from __future__ import annotations

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
) -> list[TestResult]:
    """Parse a Mocha JUnit XML string into :class:`TestResult` objects.

    Iterates over all ``<testcase>`` elements found at any depth in the
    XML tree. A test is considered passed if it has no ``<failure>`` or
    ``<error>`` child element. Test cases with no ``name`` attribute are
    skipped with a warning.

    Args:
        instance_id: Identifier of the benchmark instance that produced
                     the results.
        patch_type: The patch state under which the tests were run.
        agent_name: Name of the agent that produced the patch.
        xml_string: JUnit-format XML string produced by
                    ``mocha-junit-reporter`` to parse.

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
        passed = (
            tc.find('failure') is None and tc.find('error') is None
        )
        results.append(
            TestResult(
                instance_id=instance_id,
                patch_type=patch_type,
                agent_name=agent_name,
                test_name=test_name,
                passed=passed,
            )
        )

    return results