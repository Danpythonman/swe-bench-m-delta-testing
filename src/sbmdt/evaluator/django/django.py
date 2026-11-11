"""Evaluator for django/django instances.

Django does not use pytest. Its suite runs through ``tests/runtests.py``,
which takes dotted labels relative to ``tests/`` (``auth_tests`` for a
whole test app, ``auth_tests.test_validators`` for one module) and is
built on unittest, so outcomes are captured by the injected unittest
runner rather than by parsing the verbose console output.
"""

from __future__ import annotations

import shlex
from typing import override

from sbmdt.evaluator.python.python import INJECTED_DIR, PythonEvaluator

__all__ = [
    'DjangoEvaluator',
    'django_labels',
]


def django_labels(paths: list[str]) -> list[str]:
    """Turn test patch paths into ``runtests.py`` labels.

    A changed test module (``tests.py`` or ``test*.py``) selects just that
    module. Any other file under a test app (models, fixtures, templates)
    can affect every test in the app, so it selects the whole app, which
    also subsumes that app's module labels. Files outside a test app,
    including ``tests/runtests.py`` itself, select nothing.

    Args:
        paths: Files the instance's test patch adds or modifies.

    Returns:
        Labels in first-seen order, apps before modules.
    """
    apps: list[str] = []
    modules: list[str] = []
    for path in paths:
        parts = path.split('/')
        if len(parts) < 3 or parts[0] != 'tests':
            continue
        app = parts[1]
        name = parts[-1]
        if name.endswith('.py') and name.startswith('test'):
            label = '.'.join([*parts[1:-1], name.removesuffix('.py')])
            if label not in modules:
                modules.append(label)
        elif app not in apps:
            apps.append(app)
    return apps + [m for m in modules if m.split('.')[0] not in apps]


class DjangoEvaluator(PythonEvaluator):
    """Runs the touched Django test labels through ``runtests.py``.

    ``--parallel 1`` keeps every test in the one process the injected
    runner hooks, and ``test_sqlite`` is the settings module every Django
    checkout ships for running the suite without an external database.
    """

    @override
    def test_command(self, paths: list[str]) -> str | None:
        """Run the labels the touched paths map to.

        Args:
            paths: Files the instance's test patch adds or modifies.

        Returns:
            A ``runtests.py`` invocation, or ``None`` when no path lies in
            a test app.
        """
        labels = django_labels(paths)
        if not labels:
            return None
        return ' '.join(
            [
                f'python {INJECTED_DIR}/sbmdt_unittest_runner.py',
                'tests/runtests.py',
                '--verbosity 2',
                '--settings=test_sqlite',
                '--parallel 1',
                *map(shlex.quote, labels),
            ]
        )
