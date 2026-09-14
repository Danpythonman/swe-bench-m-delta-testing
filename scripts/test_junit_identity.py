"""Regression tests for fully-qualified JUnit test identities."""

import datetime as dt
import unittest

from sbmdt.evaluator.alibaba.karma_junit_parser import (
    results_xml_to_test_results as parse_karma,
)
from sbmdt.evaluator.base import PatchType
from sbmdt.evaluator.highlightjs.highlightjs_mocha_junit_parser import (
    results_xml_to_test_results as parse_mocha,
)

XML = """<testsuites><testsuite>
<testcase classname="suite one" name="same" />
<testcase classname="suite two" name="same"><error /></testcase>
</testsuite></testsuites>"""


class JunitIdentityTests(unittest.TestCase):
    def check_parser(self, parser):
        rows = parser(
            'instance', PatchType.GOLD, 'GOLD', XML, dt.datetime.now()
        )
        self.assertEqual(
            [row.test_name for row in rows],
            ['suite one same', 'suite two same'],
        )
        self.assertEqual([row.passed for row in rows], [True, False])

    def test_karma_identity(self):
        self.check_parser(parse_karma)

    def test_mocha_identity(self):
        self.check_parser(parse_mocha)


if __name__ == '__main__':
    unittest.main()
