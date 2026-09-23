"""The rules that decide what a reference is and which runs it grades.

Each case is one of the ways the 23 September audit found a verdict
resting on something other than the agent's patch.
"""

import importlib.util
import os
import sys
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts', 'scoring'))
import image_generation as ig  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    'test_split', os.path.join(ROOT, 'notebooks', 'test_split.py'))
ts = importlib.util.module_from_spec(_SPEC)
sys.modules['test_split'] = ts
assert _SPEC.loader is not None
_SPEC.loader.exec_module(ts)


def frame(rows, instance='demo'):
    """Long-format results from (patch_type, run, test, passed) tuples."""
    return pd.DataFrame(
        [{'instance_id': instance, 'patch_type': p, 'agent_name': p,
          'timestamp': r, 'test_name': t, 'passed': ok}
         for p, r, t, ok in rows])


class RepeatedTitleInOneRun(unittest.TestCase):
    """carbon's Public API snapshot test is reported twice per run."""

    def test_one_failing_copy_fails_the_test(self):
        book = ig.run_verdicts(frame([
            ('before_patch', 'r1', 'Public API', False),
            ('before_patch', 'r1', 'Public API', True),
        ]))
        self.assertIs(book[('demo', 'before_patch', 'r1')]['Public API'],
                      False)

    def test_order_of_the_copies_does_not_matter(self):
        book = ig.run_verdicts(frame([
            ('before_patch', 'r1', 'Public API', True),
            ('before_patch', 'r1', 'Public API', False),
        ]))
        self.assertIs(book[('demo', 'before_patch', 'r1')]['Public API'],
                      False)


NAMES_A_PASSING_TEST = '''\
+++ b/src/TimePicker-test.js
+    it('should behave readonly as expected', () => {
'''


class AnchoredTestsThatAlreadyPass(unittest.TestCase):
    """carbon-12420: the named test passes unpatched; the snapshot moves."""

    def split(self, base_named):
        rows = frame([
            ('before_patch', 'r1', 'TimePicker should behave readonly as '
             'expected', base_named),
            ('before_patch', 'r1', 'Public API', False),
            ('before_patch', 'r1', 'other', True),
            ('gold', 'r2', 'TimePicker should behave readonly as expected',
             True),
            ('gold', 'r2', 'Public API', True),
            ('gold', 'r2', 'other', True),
        ])
        return ts.classify_tests(
            rows, pre_label='before_patch', post_label='gold',
            columns=ts.Columns(run='timestamp'),
            test_patch_diffs={'demo': NAMES_A_PASSING_TEST})

    def test_falls_back_to_what_actually_failed(self):
        self.assertEqual(self.split(base_named=True).fail_to_pass['demo'],
                         ['Public API'])

    def test_a_named_test_that_fails_unpatched_is_still_the_anchor(self):
        self.assertEqual(self.split(base_named=False).fail_to_pass['demo'],
                         ['TimePicker should behave readonly as expected'])


class RunsThatStoppedPartWay(unittest.TestCase):
    """alibaba-4182 stops at test 117 of ~1,550, patched or not."""

    REF = {'demo': {'f2p': ['t0'], 'p2p': [f't{i}' for i in range(1, 20)]}}

    def runs(self, reached_by_run):
        rows = []
        for run, reached in reached_by_run.items():
            rows += [('with_image', run, f't{i}', True)
                     for i in range(reached)]
        return frame(rows)

    def test_a_short_run_is_not_graded(self):
        kept, cut = ig.drop_incomplete_runs(
            self.runs({'r1': 3}), self.REF, verbose=False)
        self.assertTrue(kept.empty)
        self.assertEqual(cut, {('demo', 'with_image', 'with_image')})

    def test_a_complete_run_in_the_same_campaign_is_used_instead(self):
        kept, cut = ig.drop_incomplete_runs(
            self.runs({'r1': 20, 'r2': 3}), self.REF, verbose=False)
        self.assertEqual(set(kept.timestamp), {'r1'})
        self.assertEqual(cut, set())

    def test_reference_runs_are_left_alone(self):
        rows = frame([('gold', 'r1', 't0', True)])
        kept, _ = ig.drop_incomplete_runs(rows, self.REF, verbose=False)
        self.assertEqual(len(kept), 1)


if __name__ == '__main__':
    unittest.main()
