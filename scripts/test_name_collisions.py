"""A test title that repeats inside one run is not a flaky test.

The JUnit parsers qualify a test by its class as well as its name, so
new results cannot collide. Results already in the parquet were written
before that, and there a title such as prettier's 'snippet: #0 format'
arrives five times for one run, one row per fixture file. Collapsing
those by name alone makes the all/any disagreement that detects
flakiness fire on the collision instead, and the name is then dropped
from the reference entirely -- which cost 17 instances their whole
FAIL_TO_PASS list.
"""

import importlib.util
import os
import sys
import unittest

import pandas as pd

_SPEC = importlib.util.spec_from_file_location(
    'test_split',
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 'notebooks', 'test_split.py'),
)
ts = importlib.util.module_from_spec(_SPEC)
sys.modules['test_split'] = ts
assert _SPEC.loader is not None
_SPEC.loader.exec_module(ts)


def frame(rows):
    """Long-format results from (patch_type, run, test, passed) tuples."""
    return pd.DataFrame(
        [{'instance_id': 'demo', 'patch_type': p, 'timestamp': r,
          'test_name': t, 'passed': ok} for p, r, t, ok in rows]
    )


# One run each side. 'dup' is reported twice per run because two files
# give a test the same title. Before the fix it fails in the pre-patch
# run and passes in the post-patch one, which is exactly a FAIL_TO_PASS.
COLLISION = frame([
    ('before_patch', 'r1', 'dup', False),
    ('before_patch', 'r1', 'dup', True),
    ('before_patch', 'r1', 'other', True),
    ('gold', 'r2', 'dup', True),
    ('gold', 'r2', 'dup', True),
    ('gold', 'r2', 'other', True),
])

# The same shape, but the disagreement is across two separate pre-patch
# runs. That is a real flaky test and must still be discarded.
FLAKY = frame([
    ('before_patch', 'r1', 'dup', False),
    ('before_patch', 'r2', 'dup', True),
    ('before_patch', 'r1', 'other', True),
    ('gold', 'r3', 'dup', True),
    ('gold', 'r3', 'other', True),
])


class NameCollisionTests(unittest.TestCase):

    def test_collision_is_not_flaky_when_the_run_is_known(self):
        split = ts.classify_tests(
            COLLISION, pre_label='before_patch', post_label='gold',
            columns=ts.Columns(run='timestamp'),
        )
        self.assertEqual(split.fail_to_pass.get('demo'), ['dup'])
        self.assertEqual(split.flaky, {})

    def test_collision_is_lost_without_the_run_column(self):
        """The old behaviour, kept explicit so the fix cannot regress."""
        split = ts.classify_tests(
            COLLISION, pre_label='before_patch', post_label='gold',
        )
        self.assertNotIn('demo', split.fail_to_pass)
        self.assertIn('demo', split.flaky)

    def test_a_genuinely_flaky_test_is_still_dropped(self):
        split = ts.classify_tests(
            FLAKY, pre_label='before_patch', post_label='gold',
            columns=ts.Columns(run='timestamp'),
        )
        self.assertNotIn('demo', split.fail_to_pass)
        self.assertEqual(split.flaky.get('demo'), ['dup'])

    def test_unique_names_are_unaffected(self):
        """With qualified names the collapse is a no-op, as it should be."""
        rows = frame([
            ('before_patch', 'r1', 'a', False),
            ('before_patch', 'r1', 'b', True),
            ('gold', 'r2', 'a', True),
            ('gold', 'r2', 'b', True),
        ])
        plain = ts.classify_tests(
            rows, pre_label='before_patch', post_label='gold')
        keyed = ts.classify_tests(
            rows, pre_label='before_patch', post_label='gold',
            columns=ts.Columns(run='timestamp'))
        self.assertEqual(plain.fail_to_pass, keyed.fail_to_pass)
        self.assertEqual(plain.pass_to_pass, keyed.pass_to_pass)


if __name__ == '__main__':
    unittest.main()
