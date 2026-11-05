"""Run SymPy's own test runner while recording every test outcome.

Copied into the container and invoked as::

    python sbmdt_sympy_runner.py bin/test [args...]

SymPy's images ship no pytest; its suite runs through ``bin/test``, whose
results all pass through a ``PyTestReporter``. This hooks that reporter
and then executes ``bin/test`` as ``__main__``, so every outcome is
captured without parsing console output. The reporter lives in
``sympy.testing.runtests`` in newer releases and
``sympy.utilities.runtests`` in older ones; its hook names are the same.

It runs under the repository's own interpreter, so it avoids f-strings
and anything newer than Python 3.6.

Each line is ``{"test": <path>::<function>, "outcome": <outcome>}``
where outcome is one of ``passed``, ``failed``, ``skipped``, ``xfailed``
or ``collect_error``.
"""

import json
import os
import runpy
import sys

_RESULTS_FILE = os.environ['SBMDT_RESULTS_FILE']


def _record(test, outcome):
    with open(_RESULTS_FILE, 'a') as handle:
        handle.write(json.dumps({'test': test, 'outcome': outcome}) + '\n')


def _reporter_class():
    try:
        from sympy.testing.runtests import PyTestReporter
    except ImportError:
        from sympy.utilities.runtests import PyTestReporter
    return PyTestReporter


def _hook(cls):
    state = {'file': None, 'test': None}

    def current():
        if state['test'] is None:
            # Raised while executing the module itself, before any test.
            return state['file']
        return '{}::{}'.format(state['file'], state['test'])

    def wrap(name, before):
        original = getattr(cls, name)

        def wrapper(self, *args, **kwargs):
            before(self, *args, **kwargs)
            return original(self, *args, **kwargs)

        setattr(cls, name, wrapper)

    def entering_filename(self, filename, n):
        state['file'] = os.path.relpath(filename, os.getcwd())
        state['test'] = None

    def entering_test(self, f):
        state['test'] = f.__name__

    def outcome(name):
        def before(self, *args, **kwargs):
            # A failure outside any test means the module itself broke; a
            # skip there (a missing optional dependency) skips the file.
            if state['test'] is None and name == 'failed':
                _record(current(), 'collect_error')
            else:
                _record(current(), name)

        return before

    def import_error(self, filename, exc_info):
        _record(os.path.relpath(filename, os.getcwd()), 'collect_error')

    wrap('entering_filename', entering_filename)
    wrap('entering_test', entering_test)
    wrap('test_pass', outcome('passed'))
    wrap('test_fail', outcome('failed'))
    wrap('test_exception', outcome('failed'))
    wrap('test_skip', outcome('skipped'))
    wrap('test_xfail', outcome('xfailed'))
    # SymPy does not count an unexpected pass as a failure.
    wrap('test_xpass', outcome('passed'))
    wrap('import_error', import_error)


def main():
    script = os.path.abspath(sys.argv[1])
    sys.argv = [script] + sys.argv[2:]
    # As when running the script directly: its directory first on sys.path
    # (bin/test imports its get_sympy helper from there), with /testbed
    # importable so the reporter can be hooked before the script runs.
    sys.path[0] = os.path.dirname(script)
    sys.path.insert(1, os.getcwd())
    _hook(_reporter_class())
    runpy.run_path(script, run_name='__main__')


if __name__ == '__main__':
    main()
