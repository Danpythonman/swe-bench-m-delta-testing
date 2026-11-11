"""A test missing from the before run is not necessarily a failing test.

Each case is a shape found in the reference runs: the before run can lose a
test because its file failed to import (pylint-4551), its class fixture
crashed (django-14011), its describe() block threw (prettier-9514), the
test patch added it to a file that could not load, or the runner simply
stopped (alibaba next-2131, where an "after each" hook failed and mocha
abandoned the rest of the file). The first four are ERROR and count as
failing, as in the benchmarks' own splits; the last is NOT_EXECUTED and
gets no label.
"""

import unittest

import pandas as pd

import sbmdt.analysis.test_split as ts


def frame(rows, instance='demo', file=None):
    """Long-format results from (patch_type, test, passed[, file]) tuples."""
    return pd.DataFrame(
        [{'instance_id': instance, 'patch_type': r[0], 'agent_name': r[0],
          'timestamp': r[0], 'test_name': r[1], 'passed': r[2],
          'test_file': r[3] if len(r) > 3 else file}
         for r in rows])


def split(rows, rule=ts.ABSENT_BY_EVIDENCE, patches=None):
    return ts.classify_tests(
        frame(rows), pre_label='before', post_label='gold',
        columns=ts.Columns(run='timestamp', file='test_file'),
        min_pre_run_coverage=0, absent_before=rule,
        test_patches=patches or {})


class ErrorRowsCount(unittest.TestCase):
    def test_pytest_collection_error_covers_the_file(self):
        s = split([
            ('before', 'tests/test_x.py', False),
            ('gold', 'tests/test_x.py::test_new', True),
            ('gold', 'tests/test_x.py::TestOld::test_old', True),
        ])
        self.assertEqual(
            s.fail_to_pass['demo'],
            ['tests/test_x.py::TestOld::test_old',
             'tests/test_x.py::test_new'])
        self.assertEqual(len(s.errored['demo']), 2)
        self.assertEqual(s.not_executed, {})

    def test_unittest_setupclass_error_covers_the_class(self):
        s = split([
            ('before', 'setUpClass (servers.tests.LiveServerViews)', False),
            ('before', 'test_other (servers.tests.Elsewhere)', True),
            ('gold', 'test_view (servers.tests.LiveServerViews)', True),
            ('gold', 'test_other (servers.tests.Elsewhere)', True),
        ])
        self.assertEqual(s.fail_to_pass['demo'],
                         ['test_view (servers.tests.LiveServerViews)'])
        self.assertEqual(s.pass_to_pass['demo'],
                         ['test_other (servers.tests.Elsewhere)'])

    def test_jest_declaration_exception_covers_its_block(self):
        s = split([
            ('before', 'a.js encountered a declaration exception', False,
             'spec.js'),
            ('gold', 'a.js [meriyah] format', True, 'spec.js'),
        ])
        self.assertEqual(s.fail_to_pass['demo'], ['a.js [meriyah] format'])

    def test_declaration_exception_does_not_cover_another_file(self):
        s = split([
            ('before', 'a.js encountered a declaration exception', False,
             'one.js'),
            ('gold', 'a.js [meriyah] format', True, 'two.js'),
        ])
        self.assertEqual(s.fail_to_pass, {})
        self.assertEqual(s.not_executed['demo'], ['a.js [meriyah] format'])

    def test_mocha_before_hook_covers_its_block(self):
        s = split([
            ('before', 'Tree "before all" hook for "renders"', False),
            ('gold', 'Tree renders', True),
        ])
        self.assertEqual(s.fail_to_pass['demo'], ['Tree renders'])


class AddedByTheTestPatch(unittest.TestCase):
    PATCH = (
        'diff --git a/test/tree-spec.js b/test/tree-spec.js\n'
        '--- a/test/tree-spec.js\n+++ b/test/tree-spec.js\n'
        "+    it('should keep the selected node', () => {\n"
    )

    def test_a_test_the_patch_adds_counts_as_failing(self):
        s = split([
            ('before', 'Tree renders', True),
            ('gold', 'Tree renders', True),
            ('gold', 'Tree should keep the selected node', True),
        ], patches={'demo': self.PATCH})
        self.assertEqual(s.fail_to_pass['demo'],
                         ['Tree should keep the selected node'])
        self.assertEqual(s.not_executed, {})


class RunnerStoppedEarly(unittest.TestCase):
    ROWS = [
        ('before', 'Tree renders', True),
        ('before', 'Tree breaks', False),
        ('before', 'Tree "after each" hook for "breaks"', False),
        ('gold', 'Tree renders', True),
        ('gold', 'Tree breaks', True),
        ('gold', 'Tree never reached', True),
    ]

    def test_an_unexplained_absence_gets_no_label(self):
        s = split(self.ROWS)
        self.assertEqual(s.fail_to_pass['demo'], ['Tree breaks'])
        self.assertEqual(s.not_executed['demo'], ['Tree never reached'])
        self.assertEqual(s.pass_to_pass['demo'], ['Tree renders'])

    def test_the_original_rule_still_counts_it_as_failing(self):
        s = split(self.ROWS, rule=ts.ABSENT_AS_FAIL)
        self.assertEqual(s.fail_to_pass['demo'],
                         ['Tree breaks', 'Tree never reached'])
        self.assertEqual(s.not_executed, {})


class AbsentStates(unittest.TestCase):
    def test_states_for_one_run(self):
        states = ts.absent_states(
            ['pkg/test_a.py::test_one', 'pkg/test_b.py::test_two'],
            failed={'pkg/test_a.py'}, files={}, test_patch=None)
        self.assertEqual(states, {
            'pkg/test_a.py::test_one': ts.ERROR,
            'pkg/test_b.py::test_two': ts.NOT_EXECUTED,
        })

    def test_python_test_added_by_the_patch(self):
        patch = ('diff --git a/tests/test_v.py b/tests/test_v.py\n'
                 '+def test_is_pypy():\n')
        states = ts.absent_states(
            ['tests/test_v.py::test_is_pypy', 'tests/test_v.py::test_old'],
            failed=set(), files={}, test_patch=patch)
        self.assertEqual(states['tests/test_v.py::test_is_pypy'], ts.ERROR)
        self.assertEqual(states['tests/test_v.py::test_old'], ts.NOT_EXECUTED)


if __name__ == '__main__':
    unittest.main()
