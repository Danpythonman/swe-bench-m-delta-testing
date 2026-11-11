"""pytest plugin that records one JSON line per test phase outcome.

Copied into the container and loaded with ``-p sbmdt_pytest_reporter``.
It runs under the repository's own interpreter, which can be as old as
Python 3.6, so it avoids f-strings and anything newer.

Each line is ``{"test": <nodeid>, "outcome": <outcome>}`` where outcome
is one of ``passed``, ``failed``, ``skipped``, ``xfailed`` or
``collect_error``. Lines are flushed as they are written, so a run killed
by a timeout still leaves every result recorded up to that point.
"""

import json
import os

_RESULTS_FILE = os.environ['SBMDT_RESULTS_FILE']


def _record(test, outcome):
    with open(_RESULTS_FILE, 'a') as handle:
        handle.write(json.dumps({'test': test, 'outcome': outcome}) + '\n')


def pytest_runtest_logreport(report):
    # A passing setup or teardown says nothing on its own; the call phase
    # carries the verdict. A failing or skipping setup/teardown does count,
    # since the call phase then never ran or the test left a mess behind.
    if report.when != 'call' and report.outcome == 'passed':
        return
    if hasattr(report, 'wasxfail'):
        # Expected failure that failed: the test behaved as declared.
        # A non-strict unexpected pass reports as passed; a strict one
        # reports as failed. Both flow through unchanged below.
        if report.outcome == 'skipped':
            _record(report.nodeid, 'xfailed')
            return
    _record(report.nodeid, report.outcome)


def pytest_collectreport(report):
    # A module that fails to import contributes no tests at all, so its
    # tests are simply absent from the run. Recording the error keeps that
    # visible instead of silent.
    if report.outcome == 'failed':
        _record(report.nodeid, 'collect_error')
