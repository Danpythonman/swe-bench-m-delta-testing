"""Evaluator for sympy/sympy instances.

SymPy's images ship no pytest. Its suite runs through ``bin/test``, so
outcomes are captured by the injected SymPy runner, which hooks the
runner's reporter rather than parsing the console output.
"""

from __future__ import annotations

import shlex
from typing import Final, override

from sbmdt.evaluator.python.python import INJECTED_DIR, PythonEvaluator
from sbmdt.evaluator.python.selection import is_python_test_module

__all__ = [
    'SympyEvaluator',
]

# Some tests draw random inputs, and some SymPy internals order terms by
# hash. Fixing both seeds gives the before and after runs the same inputs
# and orderings, so a difference between them is the patch's doing.
SEED: Final[int] = 0


class SympyEvaluator(PythonEvaluator):
    """Runs the touched SymPy test modules through ``bin/test``."""

    @override
    def test_command(self, paths: list[str]) -> str | None:
        """Run every touched test module.

        Args:
            paths: Files the instance's test patch adds or modifies.

        Returns:
            A ``bin/test`` invocation, or ``None`` when no path is a test
            module.
        """
        modules = [path for path in paths if is_python_test_module(path)]
        if not modules:
            return None
        return ' '.join(
            [
                f'env PYTHONHASHSEED={SEED}',
                f'python {INJECTED_DIR}/sbmdt_sympy_runner.py',
                'bin/test',
                # Otherwise bin/test re-runs itself in a child process to
                # randomise hashing, out of reach of the injected hooks.
                '--no-subprocess',
                '--verbose',
                '--no-colors',
                f'--seed {SEED}',
                *map(shlex.quote, modules),
            ]
        )
