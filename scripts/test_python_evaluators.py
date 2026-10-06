"""Regression tests for the SWE-bench Verified (Python) evaluation layer."""

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from sbmdt.benchmark import Benchmark, benchmark_of
from sbmdt.evaluator.base import PatchType
from sbmdt.evaluator.django.django import django_labels
from sbmdt.evaluator.python.python import results_to_test_results
from sbmdt.evaluator.python.selection import (
    DEFAULT_PYTHON_FILES,
    changed_paths,
    is_python_test_module,
    matches_python_files,
    python_files_patterns,
)
from sbmdt.patches import is_test_path

sys.path.insert(0, str(Path(__file__).parent))
from compare_official_split import (  # noqa: E402
    matching_keys,
    short_descriptions,
)

INJECTED = Path(__file__).parents[1] / 'src/sbmdt/evaluator/python/injected'


def fold(*records: tuple[str, str]) -> dict[str, bool]:
    jsonl = '\n'.join(
        json.dumps({'test': test, 'outcome': outcome})
        for test, outcome in records
    )
    rows = results_to_test_results(
        jsonl, 'i', PatchType.GOLD, 'GOLD', dt.datetime.now(dt.UTC)
    )
    return {row.test_name: row.passed for row in rows}


class FoldTests(unittest.TestCase):
    def test_any_failure_fails_the_test(self):
        # pytest: call passed, teardown errored.
        self.assertEqual(fold(('t', 'passed'), ('t', 'failed')), {'t': False})
        # unittest: failing subtest recorded before the parent's verdict.
        self.assertEqual(fold(('t', 'failed'), ('t', 'passed')), {'t': False})

    def test_skip_only_is_dropped_and_xfail_passes(self):
        self.assertEqual(
            fold(('s', 'skipped'), ('x', 'xfailed'), ('c', 'collect_error')),
            {'x': True, 'c': False},
        )

    def test_unknown_outcome_is_rejected(self):
        with self.assertRaises(ValueError):
            fold(('t', 'exploded'))


class SelectionTests(unittest.TestCase):
    DIFF = textwrap.dedent(
        """\
        diff --git a/tests/a/test_x.py b/tests/a/test_x.py
        --- a/tests/a/test_x.py
        +++ b/tests/a/test_x.py
        diff --git a/tests/a/old.py b/tests/a/old.py
        deleted file mode 100644
        --- a/tests/a/old.py
        +++ /dev/null
        """
    )

    def test_changed_paths_skips_deleted_files(self):
        self.assertEqual(changed_paths(self.DIFF), ['tests/a/test_x.py'])

    def test_python_test_modules(self):
        self.assertTrue(is_python_test_module('pkg/tests/test_core.py'))
        self.assertTrue(is_python_test_module('testing/python/raises_test.py'))
        self.assertFalse(is_python_test_module('tests/roots/test-x/conf.py'))
        self.assertFalse(is_python_test_module('tests/conftest.py'))

    def test_pytest_repo_testing_dir_is_a_test_path(self):
        self.assertTrue(is_test_path('testing/test_assertion.py'))


class PythonFilesTests(unittest.TestCase):
    def test_config_precedence_and_defaults(self):
        pylint = {'setup.cfg': '[tool:pytest]\npython_files = *test_*.py\n'}
        self.assertEqual(python_files_patterns(pylint), ('*test_*.py',))
        pytest_repo = {
            'tox.ini': '[tox]\n[pytest]\npython_files = test_*.py '
            '*_test.py testing/*/*.py\n',
            'setup.cfg': '[tool:pytest]\npython_files = ignored_*.py\n',
        }
        self.assertEqual(
            python_files_patterns(pytest_repo),
            ('test_*.py', '*_test.py', 'testing/*/*.py'),
        )
        # A pytest section without python_files means the default.
        sphinx = {'setup.cfg': '[tool:pytest]\nfilterwarnings = all\n'}
        self.assertEqual(python_files_patterns(sphinx), DEFAULT_PYTHON_FILES)
        toml = {
            'pyproject.toml': '[tool.pytest.ini_options]\n'
            'python_files = ["check_*.py"]\n'
        }
        self.assertEqual(python_files_patterns(toml), ('check_*.py',))
        self.assertEqual(python_files_patterns({}), DEFAULT_PYTHON_FILES)

    def test_matching(self):
        self.assertTrue(
            matches_python_files(
                'tests/unittest_pyreverse_writer.py', ('*test_*.py',)
            )
        )
        self.assertTrue(
            matches_python_files(
                'testing/python/integration.py', ('testing/*/*.py',)
            )
        )
        self.assertFalse(
            matches_python_files('tests/conftest.py', DEFAULT_PYTHON_FILES)
        )


class DjangoLabelTests(unittest.TestCase):
    def test_module_and_app_labels(self):
        self.assertEqual(
            django_labels(
                [
                    'tests/auth_tests/test_validators.py',
                    'tests/forms_tests/field_tests/test_charfield.py',
                    'tests/migrations/models.py',
                    'tests/migrations/test_operations.py',
                    'tests/runtests.py',
                    'django/db/models/base.py',
                ]
            ),
            [
                'migrations',
                'auth_tests.test_validators',
                'forms_tests.field_tests.test_charfield',
            ],
        )

    def test_tests_py_module(self):
        self.assertEqual(
            django_labels(['tests/basic/tests.py']), ['basic.tests']
        )


class MatchingKeyTests(unittest.TestCase):
    def test_official_spellings(self):
        # pytest labels are published cut at the first space.
        self.assertEqual(
            matching_keys(
                'pylint-dev__pylint-4551',
                "t.py::test_x[a: str = 'a'-str]",
                {},
            ),
            ('t.py::test_x[a:',),
        )
        # SymPy labels are bare function names.
        self.assertEqual(
            matching_keys('sympy__sympy-24562', 'a/test_n.py::test_issue', {}),
            ('test_issue',),
        )
        # Django labels keep their spaces, and newer instances use
        # unittest's Python 3.11 name, method (module.Class.method).
        name = 'test_v (auth_tests.test_validators.T)'
        py311 = 'test_v (auth_tests.test_validators.T.test_v)'
        self.assertEqual(
            matching_keys('django__django-11099', name, {}), (name, py311)
        )
        # A documented Django test is published under either spelling.
        self.assertEqual(
            matching_keys('django__django-1', name, {name: 'Checks v.'}),
            (name, py311, 'Checks v.'),
        )

    def test_short_descriptions(self):
        source = textwrap.dedent(
            """\
            class Base:
                def test_inherited(self):
                    \"\"\"
                    Inherited docs.

                    More text.
                    \"\"\"

            class T(Base):
                def test_doc(self):
                    "One line."
                def test_plain(self):
                    pass
            """
        )
        self.assertEqual(
            short_descriptions(source),
            {
                ('Base', 'test_inherited'): 'Inherited docs.',
                ('T', 'test_inherited'): 'Inherited docs.',
                ('T', 'test_doc'): 'One line.',
            },
        )


class BenchmarkTests(unittest.TestCase):
    def test_prefixes(self):
        self.assertEqual(
            benchmark_of('django__django-11099'), Benchmark.SWE_BENCH_VERIFIED
        )
        self.assertEqual(
            benchmark_of('scikit-learn__scikit-learn-10297'),
            Benchmark.SWE_BENCH_VERIFIED,
        )
        self.assertEqual(
            benchmark_of('prettier__prettier-12177'), Benchmark.SWE_BENCH_M
        )


class UnittestRunnerTests(unittest.TestCase):
    """Runs the injected runner for real against a throwaway suite."""

    SUITE = textwrap.dedent(
        """\
        import unittest

        class T(unittest.TestCase):
            def test_pass(self):
                '''A docstring that verbose output would show instead.'''
            def test_fail(self):
                self.fail()
            @unittest.skip('no')
            def test_skip(self):
                pass
            @unittest.expectedFailure
            def test_xfail(self):
                self.fail()
            def test_subtests(self):
                for i in range(2):
                    with self.subTest(i=i):
                        self.assertEqual(i, 0)

        if __name__ == '__main__':
            unittest.main(module='suite', verbosity=2)
        """
    )

    def test_records_every_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'suite.py'
            script.write_text(self.SUITE)
            results = Path(tmp) / 'results.jsonl'
            subprocess.run(
                [
                    sys.executable,
                    str(INJECTED / 'sbmdt_unittest_runner.py'),
                    str(script),
                ],
                env={**os.environ, 'SBMDT_RESULTS_FILE': str(results)},
                capture_output=True,
                check=False,
            )
            rows = results_to_test_results(
                results.read_text(),
                'i',
                PatchType.GOLD,
                'GOLD',
                dt.datetime.now(dt.UTC),
            )
        self.assertEqual(
            {row.test_name: row.passed for row in rows},
            {
                'test_pass (suite.T)': True,
                'test_fail (suite.T)': False,
                'test_xfail (suite.T)': True,
                'test_subtests (suite.T)': False,
            },
        )


if __name__ == '__main__':
    unittest.main()
