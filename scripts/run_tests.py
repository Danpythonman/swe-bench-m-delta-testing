"""Run every test in `scripts/`, and say what it skipped and why.

There was no way to run the suite as a suite. `unittest discover` is
the obvious candidate and it does not work here: `scripts/` is not a
package, and it holds command-line tools alongside the tests, so
discovery imports a tool, the tool parses `sys.argv`, and argparse
calls `SystemExit` before a single assertion runs. That is how
`test_results_json_to_parquet.py` -- a converter, not a test -- came to
look like a failing test. It is now `results_json_to_parquet.py`, and
this runner selects on content rather than trusting the name again.

Usage:
    python scripts/run_tests.py [-v] [pattern ...]
"""

from __future__ import annotations

import argparse
import ast
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent


def defines_tests(path: Path) -> bool:
    """Does this file actually define a `unittest.TestCase`?

    Parsed rather than imported, because importing is the thing that is
    unsafe: a module with top-level argument parsing exits the process.
    """
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.ClassDef)
        and any(
            getattr(base, 'attr', getattr(base, 'id', '')) == 'TestCase'
            for base in node.bases
        )
        for node in ast.walk(tree)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('patterns', nargs='*',
                        help='substrings; only matching files run')
    parser.add_argument('-v', '--verbose', action='count', default=1)
    args = parser.parse_args()

    # `src` for the package, root for `notebooks.test_split`.
    for entry in (str(ROOT / 'src'), str(ROOT)):
        if entry not in sys.path:
            sys.path.insert(0, entry)

    candidates = sorted(SCRIPTS.glob('test_*.py'))
    selected, skipped = [], []
    for path in candidates:
        if args.patterns and not any(p in path.name for p in args.patterns):
            continue
        (selected if defines_tests(path) else skipped).append(path)

    if skipped:
        print('not test modules, skipped:')
        for path in skipped:
            print(f'   {path.name}')
        print()

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    failed_to_load = []
    for path in selected:
        name = path.stem
        try:
            module = __import__(name)
        except Exception as error:  # noqa: BLE001 - reported, not raised
            # A module that will not import is a failure to report, not
            # a reason to abandon the other files.
            failed_to_load.append((name, error))
            continue
        suite.addTests(loader.loadTestsFromModule(module))

    result = unittest.TextTestRunner(verbosity=args.verbose).run(suite)

    if failed_to_load:
        print()
        print('modules that could not be imported:')
        for name, error in failed_to_load:
            print(f'   {name:<34} {type(error).__name__}: {error}')

    return 0 if result.wasSuccessful() and not failed_to_load else 1


if __name__ == '__main__':
    sys.exit(main())
