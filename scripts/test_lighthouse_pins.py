"""The two lighthouse setup steps that silently produced no results.

Both failed the same way: a literal taken from one revision of the repo
and applied to every revision. Neither raised anything a reader would
connect to the cause -- one raised "Could not find target string", the
other let tsc exit 2 -- and both left the instance with no test rows at
all, which looks identical to a machine that never ran.

The fixtures here are the real files, quoted from the commits named in
each docstring, not invented shapes.
"""

import re
import unittest

RESULTS_DIR = '/tmp/results'
MOCHA_FILES = "$(find $1/test -name '*-test.js')"


def rewrite(source):
    """The reporter injection from LighthouseEvaluator.setup, step 4."""
    reporter = (
        ' --reporter mocha-junit-reporter'
        f' --reporter-options mochaFile={RESULTS_DIR}/$1.xml'
    )
    timeout = '' if '--timeout' in source else ' --timeout 60000'
    if MOCHA_FILES not in source:
        raise AssertionError('anchor missing')
    return source.replace(MOCHA_FILES, MOCHA_FILES + reporter + timeout, 1)


def pin(declared):
    """The @types/node floor from LighthouseEvaluator.setup."""
    found = re.search(r'\d+\.\d+\.\d+', declared or '')
    return found.group() if found else '6.0.45'


# The three spellings of the mocha line across the 18 lighthouse
# instances that ship a run-mocha.sh, with how many use each.
# 13 of the 18 use this spelling.
CURRENT = (
    "  mocha --reporter dot $2 $(find $1/test -name '*-test.js')"
    " --timeout 60000;"
)
# 4 more drop the dot reporter.
NO_DOT = (
    "  mocha $2 $(find $1/test -name '*-test.js') --timeout 60000;"
)
# 1, lighthouse-4301, carries no timeout at all.
NO_TIMEOUT = (
    "  mocha --reporter dot $2 $(find $1/test -name '*-test.js');"
)


class RunMochaRewriteTests(unittest.TestCase):

    def test_every_spelling_gets_the_reporter(self):
        for source in (CURRENT, NO_DOT, NO_TIMEOUT):
            got = rewrite(source)
            self.assertIn(
                f'--reporter mocha-junit-reporter --reporter-options '
                f'mochaFile={RESULTS_DIR}/$1.xml',
                got,
                source,
            )

    def test_4301_is_reachable_at_all(self):
        """The regression. This is the line the old anchor could not find."""
        self.assertNotIn('--timeout 60000;', NO_TIMEOUT)
        self.assertIn(MOCHA_FILES, NO_TIMEOUT)
        self.assertIn('--timeout 60000', rewrite(NO_TIMEOUT))

    def test_an_existing_timeout_is_not_duplicated(self):
        for source in (CURRENT, NO_DOT):
            self.assertEqual(rewrite(source).count('--timeout'), 1, source)

    def test_the_file_list_still_precedes_the_flags(self):
        """mocha takes the specs as positional args; order must survive."""
        for source in (CURRENT, NO_DOT, NO_TIMEOUT):
            got = rewrite(source)
            self.assertLess(
                got.index(MOCHA_FILES),
                got.index('--reporter-options'),
            )

    def test_the_trailing_semicolon_survives(self):
        for source in (CURRENT, NO_DOT, NO_TIMEOUT):
            self.assertTrue(rewrite(source).rstrip().endswith(';'), source)


class TypesNodePinTests(unittest.TestCase):

    def test_an_exact_declaration_is_honoured(self):
        """lighthouse-3692 declares 6.0.66, and needs it.

        6.0.66 is the release that added `isTTY?: boolean` to
        WritableStream. sentry-prompt.ts line 19 reads
        `if (!process.stdout.isTTY || process.env.CI)`, so under the old
        flat 6.0.45 pin tsc raised TS2339 and build-cli exited 2.
        """
        self.assertEqual(pin('6.0.66'), '6.0.66')

    def test_a_range_still_collapses_to_its_floor(self):
        """Ten manifests say "^6.0.45"; the caret is the problem there."""
        self.assertEqual(pin('^6.0.45'), '6.0.45')

    def test_no_declaration_falls_back_to_the_baseline(self):
        for declared in (None, '', '*', 'latest'):
            self.assertEqual(pin(declared), '6.0.45')


class SourceStaysInSyncTests(unittest.TestCase):
    """`rewrite` and `pin` above are copies, and copies rot.

    lighthouse.py cannot be imported here: sbmdt needs Python 3.13 for
    `from warnings import deprecated` and this suite runs on 3.11. So
    assert against the source text instead. It is a weaker guarantee
    than importing the real function, and it is deliberately narrow --
    it catches the anchor or the fallback being edited in one place and
    not the other, which is exactly how the original bug was introduced.
    """

    @staticmethod
    def source():
        import os
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(here, 'src', 'sbmdt', 'evaluator',
                            'lighthouse', 'lighthouse.py')
        return open(path, encoding='utf-8').read()

    def test_the_mocha_anchor_matches(self):
        self.assertIn(
            f'mocha_files = "{MOCHA_FILES}"',
            self.source(),
        )

    def test_the_types_node_fallback_matches(self):
        text = self.source()
        self.assertIn("types_node_match.group() if types_node_match "
                      "else '6.0.45'", text)
        self.assertIn(r"r'\d+\.\d+\.\d+', "
                      "dependencies.get('@types/node')", text)

    def test_the_flat_pin_is_gone(self):
        """The exact line that cost lighthouse-3692 its whole run."""
        self.assertNotIn('npm install @types/node@6.0.45 --save-exact',
                         self.source())

    def test_the_old_mocha_anchor_is_gone(self):
        self.assertNotIn("find='--timeout 60000;'", self.source())


if __name__ == '__main__':
    unittest.main()
