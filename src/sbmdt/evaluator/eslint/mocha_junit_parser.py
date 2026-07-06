"""
Parser for JUnit XML produced by ``mocha-junit-reporter``.

``mocha-junit-reporter`` writes a standard JUnit XML file whose top-level
element is ``<testsuites>``, containing one ``<testsuite>`` per Mocha
describe-block and one ``<testcase>`` per ``it()`` call.  A test is
considered failed when its ``<testcase>`` element contains a ``<failure>``
or ``<error>`` child; otherwise it is considered passed.

Example output shape::

    <testsuites name="Mocha Tests" ...>
      <testsuite name="indent" tests="3" failures="1" ...>
        <testcase classname="indent" name="valid ..." time="0.001" />
        <testcase classname="indent" name="invalid ..." time="0.002">
          <failure message="...">...</failure>
        </testcase>
      </testsuite>
    </testsuites>
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from sbmdt.evaluator.base import PatchType, TestResult

__all__ = [
    'results_xml_to_test_results',
]


def results_xml_to_test_results(
    instance_id: str,
    patch_type: PatchType,
    agent_name: str,
    xml_content: str,
) -> list[TestResult]:
    """Parse a JUnit XML string into a list of :class:`TestResult` objects.

    Each ``<testcase>`` element in the XML becomes exactly one
    :class:`TestResult`.  The test is marked as passed unless the
    ``<testcase>`` contains a ``<failure>`` or ``<error>`` child element.

    The ``name`` used for :attr:`TestResult.test_name` is constructed as
    ``"{classname} {name}"`` from the ``<testcase>`` attributes, matching
    the ``"suite-name test-name"`` format produced by
    ``mocha-junit-reporter``.  When ``classname`` is absent or identical
    to ``name`` (some reporter configurations omit it or repeat it), only
    ``name`` is used to avoid duplication.

    Args:
        instance_id: Benchmark instance identifier, forwarded verbatim to
            each :class:`TestResult`.
        patch_type: Patch state under which the tests were run.
        agent_name: Name of the agent that produced the patch.
        xml_content: Raw JUnit XML string as returned by
            :func:`sbmdt.utils.read_from_container`.

    Returns:
        A list of :class:`TestResult`, one per ``<testcase>`` element found
        in the XML.  Returns an empty list when the XML contains no
        ``<testcase>`` elements.

    Raises:
        xml.etree.ElementTree.ParseError: If ``xml_content`` is not valid XML.
    """

    root = ET.fromstring(xml_content)

    # The root may be <testsuites> (multiple suites) or a bare <testsuite>.
    if root.tag == 'testsuite':
        testcase_elements = root.iter('testcase')
    else:
        # <testsuites> or any other wrapper — iterate all descendant testcases.
        testcase_elements = root.iter('testcase')

    results: list[TestResult] = []

    for testcase in testcase_elements:
        classname = testcase.get('classname', '')
        name = testcase.get('name', '')

        # Build a human-readable test name.  Avoid "foo foo" when classname
        # and name are identical (mocha-junit-reporter sometimes emits this
        # for top-level describe blocks).
        if classname and classname != name:
            test_name = f'{classname} {name}'
        else:
            test_name = name

        # A testcase is failed when it carries a <failure> or <error> child.
        passed = (
            testcase.find('failure') is None and testcase.find('error') is None
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
