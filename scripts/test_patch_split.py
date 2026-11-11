"""Cover `sbmdt.patches`, which decides what each run is allowed to see.

This module had no tests at all, and it is the one that answers the
question the whole benchmark rests on: which half of the gold patch is
the fix (withheld from a model run) and which half is the tests
(applied to every run). Getting that wrong in the generous direction
hands a model the answer; getting it wrong in the strict direction
marks a correct fix as failing.

The unit tests below pin the rules. The corpus tests at the bottom run
the real functions over all 510 `gold_patch.diff` files, because the
rules are regular expressions over paths and the only honest check of a
path rule is the paths that actually occur.
"""

import re
import unittest
from collections import Counter
from pathlib import Path

from sbmdt.env import DOCKERFILES_BASE
from sbmdt.patches import (
    drop_mode_only_sections,
    drop_unappliable_binary,
    is_test_path,
    split_diff,
    split_diff_by_file,
)

HDR = re.compile(r'^diff --git a/(\S+) b/(\S+)', re.M)


def s(path, body='@@ -1 +1 @@\n-a\n+b\n'):
    """A minimal per-file section for `path`."""
    return (f'diff --git a/{path} b/{path}\n'
            f'--- a/{path}\n+++ b/{path}\n{body}')


class IsTestPathTests(unittest.TestCase):

    def test_test_directories_count(self):
        for path in (
            'test/foo.js', 'tests/foo.js', 'spec/foo.js', 'specs/foo.js',
            'src/__tests__/foo.js', 'src/__test__/foo.js',
            'e2e/foo.js', 'cypress/foo.js', 'a/b/test/c/d.js',
        ):
            with self.subTest(path=path):
                self.assertTrue(is_test_path(path))

    def test_test_suffixes_count(self):
        for path in (
            'src/Foo-test.js', 'src/Foo.test.js', 'src/Foo_spec.ts',
            'src/Foo.spec.tsx', 'src/Foo-test.mjs', 'src/Foo.test.cjs',
            'conftest.py', 'pkg/test_thing.py', 'pkg/thing_test.py',
        ):
            with self.subTest(path=path):
                self.assertTrue(is_test_path(path))

    def test_ordinary_source_is_not_a_test(self):
        # The strict direction. A false positive here would apply the
        # fix to every run and score it as the model's work.
        for path in (
            'src/index.js', 'lib/Dropdown.js', 'packages/react/src/App.tsx',
            'src/protest.js', 'src/latest.js', 'src/contest/index.js',
            'README.md', 'package.json', 'src/testing-utils.js',
        ):
            with self.subTest(path=path):
                self.assertFalse(is_test_path(path))

    def test_jest_snapshots_are_test_data(self):
        """Regression: these were routed into the code half.

        `Dropdown-test.js.snap` ends in `.snap`, so it matched none of
        the `-test.js` suffix rules, and it lives in `__snapshots__/`
        beside the component rather than under `__tests__/`, so it
        matched no directory rule either. For carbon-3610, -4260, -4999
        and -15197 it was the only test-side file in the gold patch,
        which left `test_patch.diff` empty.
        """
        for path in (
            'packages/react/src/components/Dropdown/__snapshots__/'
            'Dropdown-test.js.snap',
            'packages/react/src/components/ModalWrapper/__snapshots__/'
            'ModalWrapper-test.js.snap',
            'a/__snapshots__/anything.txt',
            'a/b/c.snap',
        ):
            with self.subTest(path=path):
                self.assertTrue(is_test_path(path))


class SplitDiffTests(unittest.TestCase):

    def test_the_two_halves_partition_the_input(self):
        diff = s('src/a.js') + s('test/a.js') + s('src/b.js')
        code, test = split_diff(diff)
        self.assertEqual(code, s('src/a.js') + s('src/b.js'))
        self.assertEqual(test, s('test/a.js'))
        self.assertEqual(len(code) + len(test), len(diff))

    def test_order_is_preserved_within_each_half(self):
        diff = s('src/z.js') + s('src/a.js')
        code, _ = split_diff(diff)
        self.assertLess(code.index('src/z.js'), code.index('src/a.js'))

    def test_text_without_a_header_is_all_code(self):
        # A bare `---`/`+++` patch has no `diff --git` line to route on.
        # Treating it as code is the safe default: it is withheld rather
        # than handed to the model.
        bare = '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n'
        self.assertEqual(split_diff(bare), (bare, ''))

    def test_empty_input(self):
        self.assertEqual(split_diff(''), ('', ''))


class SplitDiffByFileTests(unittest.TestCase):

    def test_one_entry_per_file_in_order(self):
        diff = s('a.js') + s('b.js')
        self.assertEqual(
            [p for p, _ in split_diff_by_file(diff)], ['a.js', 'b.js'])

    def test_sections_rejoin_to_the_original(self):
        diff = s('a.js') + s('b.js') + s('c.js')
        self.assertEqual(
            ''.join(sec for _, sec in split_diff_by_file(diff)), diff)

    def test_no_header_gives_nothing_to_retry(self):
        self.assertEqual(split_diff_by_file('--- a/x\n+++ b/x\n'), [])


class DropBinaryTests(unittest.TestCase):

    STUB = (
        'diff --git a/i.png b/i.png\nindex 1..2 100644\n'
        'Binary files a/i.png and b/i.png differ\n'
    )

    def test_a_stub_without_payload_is_dropped(self):
        kept, dropped = drop_unappliable_binary(self.STUB + s('a.js'))
        self.assertEqual(dropped, ['i.png'])
        self.assertEqual(kept, s('a.js'))

    def test_a_binary_section_with_its_payload_is_kept(self):
        payload = (
            'diff --git a/i.png b/i.png\nindex 1..2 100644\n'
            'GIT binary patch\nliteral 4\nzcmZ\n\n'
        )
        kept, dropped = drop_unappliable_binary(payload + s('a.js'))
        self.assertEqual(dropped, [])
        self.assertEqual(kept, payload + s('a.js'))

    def test_a_patch_with_no_headers_is_returned_untouched(self):
        self.assertEqual(drop_unappliable_binary('nonsense'), ('nonsense', []))


class DropModeOnlyTests(unittest.TestCase):
    """The rule that decides what is left of a Refact submission.

    Refact pads its patches with pure executable-bit flips -- 1945 of
    them in openlayers-14945, around a single real hunk. Dropping them
    is right, but it is also why so many of those patches end up with
    exactly one file, which skips the per-file retry in `apply_patch`.
    """

    MODE = 'diff --git a/x.js b/x.js\nold mode 100644\nnew mode 100755\n'

    def test_a_pure_mode_flip_is_dropped(self):
        kept, dropped = drop_mode_only_sections(self.MODE + s('a.js'))
        self.assertEqual(dropped, ['x.js'])
        self.assertEqual(kept, s('a.js'))

    def test_a_mode_flip_beside_a_hunk_is_kept(self):
        both = (
            'diff --git a/x.js b/x.js\nold mode 100644\nnew mode 100755\n'
            '--- a/x.js\n+++ b/x.js\n@@ -1 +1 @@\n-a\n+b\n'
        )
        kept, dropped = drop_mode_only_sections(both)
        self.assertEqual(dropped, [])
        self.assertEqual(kept, both)

    def test_a_new_file_is_not_a_mode_only_section(self):
        # `new file mode 100644` is not `old mode`/`new mode`, and
        # dropping it would delete the file the patch adds.
        new = (
            'diff --git a/n.js b/n.js\nnew file mode 100644\n'
            'index 0000000..e69de29\n'
        )
        self.assertEqual(drop_mode_only_sections(new), (new, []))

    def test_a_deleted_file_is_not_a_mode_only_section(self):
        gone = (
            'diff --git a/d.js b/d.js\ndeleted file mode 100644\n'
            'index e69de29..0000000\n'
        )
        self.assertEqual(drop_mode_only_sections(gone), (gone, []))

    def test_many_mode_sections_leave_the_one_real_file(self):
        noise = ''.join(
            f'diff --git a/n{i}.js b/n{i}.js\n'
            'old mode 100644\nnew mode 100755\n'
            for i in range(200)
        )
        kept, dropped = drop_mode_only_sections(noise + s('real.js'))
        self.assertEqual(len(dropped), 200)
        self.assertEqual(len(split_diff_by_file(kept)), 1)


class GoldCorpusTests(unittest.TestCase):
    """Run the real rules over the real 510 gold patches.

    A path rule is only as good as the paths it meets, and these are
    the paths it meets. Skipped rather than failed when the dockerfiles
    tree is not checked out, so the suite still runs on a bare clone.
    """

    @classmethod
    def setUpClass(cls):
        cls.base = Path(DOCKERFILES_BASE)
        if not cls.base.is_dir():
            raise unittest.SkipTest('dockerfiles/ not present')
        cls.golds = sorted(cls.base.glob('*/gold_patch.diff'))
        if not cls.golds:
            raise unittest.SkipTest('no gold patches on disk')

    def read(self, path):
        return path.read_text(encoding='utf-8', errors='replace')

    def test_every_gold_patch_has_a_test_half(self):
        """An empty test half means no FAIL_TO_PASS tests can be added.

        Four carbon instances failed this before `.snap` was recognised.
        """
        empty = [g.parent.name for g in self.golds
                 if not split_diff(self.read(g))[1].strip()]
        self.assertEqual(empty, [], 'gold patches with no test half')

    def test_the_split_loses_no_section(self):
        """Every byte and every file lands in exactly one half.

        The halves are compared as multisets, not as ordered lists:
        routing reorders sections relative to the original, which is
        fine, but it must never drop or duplicate one.
        """
        for g in self.golds:
            diff = self.read(g)
            code, test = split_diff(diff)
            with self.subTest(instance=g.parent.name):
                self.assertEqual(len(code) + len(test), len(diff))
                self.assertEqual(
                    Counter(HDR.findall(code)) + Counter(HDR.findall(test)),
                    Counter(HDR.findall(diff)),
                )

    def test_every_header_in_the_corpus_parses(self):
        """`DIFF_HEADER` uses `\\S+`, so a path with a space would not
        match and its section would silently merge into the one before.
        No such path exists in the corpus; this fails if one appears.
        """
        unparsed = []
        for g in self.golds:
            diff = self.read(g)
            for line in re.findall(r'^diff --git .*$', diff, re.M):
                if not HDR.match(line):
                    unparsed.append((g.parent.name, line))
        self.assertEqual(unparsed[:5], [], 'unparseable diff headers')

    def test_no_snapshot_is_routed_to_the_code_half(self):
        stray = []
        for g in self.golds:
            code, _ = split_diff(self.read(g))
            for _, b in HDR.findall(code):
                if b.endswith('.snap') or '__snapshots__/' in b:
                    stray.append((g.parent.name, b))
        self.assertEqual(stray[:5], [], 'snapshots withheld from the tests')


if __name__ == '__main__':
    unittest.main(verbosity=2)
