"""Run a unittest-based test script while recording every test outcome.

Copied into the container and invoked as::

    python sbmdt_unittest_runner.py <script> [args...]

It hooks :class:`unittest.TestResult` and then executes ``<script>`` as
``__main__``. Any runner built on unittest (Django's ``runtests.py``
included) reports through ``TestResult``, so every outcome is captured
without parsing console output, whose format varies across versions and
replaces test names with docstrings at higher verbosity.

It runs under the repository's own interpreter, which can be as old as
Python 3.6, so it avoids f-strings and anything newer.

Each line is ``{"test": <id>, "outcome": <outcome>}`` where the id is
``method (module.Class)`` and outcome is one of ``passed``, ``failed``,
``skipped`` or ``xfailed``.
"""

import json
import os
import runpy
import sys
import unittest

_RESULTS_FILE = os.environ['SBMDT_RESULTS_FILE']


def _test_id(test):
    method = getattr(test, '_testMethodName', None)
    if method is None:
        # Placeholders such as a setUpClass failure have no method name;
        # their str() already names what failed.
        return str(test)
    cls = type(test)
    return '{} ({}.{})'.format(method, cls.__module__, cls.__qualname__)


def _record(test, outcome):
    with open(_RESULTS_FILE, 'a') as handle:
        handle.write(
            json.dumps({'test': _test_id(test), 'outcome': outcome}) + '\n'
        )


def _hook(method_name, outcome):
    original = getattr(unittest.TestResult, method_name)

    def wrapper(self, test, *args, **kwargs):
        _record(test, outcome)
        return original(self, test, *args, **kwargs)

    setattr(unittest.TestResult, method_name, wrapper)


def _hook_subtests():
    original = unittest.TestResult.addSubTest

    def wrapper(self, test, subtest, err):
        # A passing subtest is not a verdict; the parent test reports
        # success itself once every subtest has passed. A failing one
        # fails the parent, which then never reports success.
        if err is not None:
            _record(test, 'failed')
        return original(self, test, subtest, err)

    unittest.TestResult.addSubTest = wrapper


def main():
    _hook('addSuccess', 'passed')
    _hook('addFailure', 'failed')
    _hook('addError', 'failed')
    _hook('addSkip', 'skipped')
    _hook('addExpectedFailure', 'xfailed')
    _hook('addUnexpectedSuccess', 'failed')
    _hook_subtests()

    script = os.path.abspath(sys.argv[1])
    sys.argv = [script] + sys.argv[2:]
    # Running a script puts its directory first on sys.path; runpy does
    # not, and runners like Django's import their settings from there.
    sys.path[0] = os.path.dirname(script)
    runpy.run_path(script, run_name='__main__')


if __name__ == '__main__':
    main()
