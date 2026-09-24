"""Drive the real `apply_patch` against real git in a temporary repo.

The previous version of this file lifted `apply_patch` out of the source
with `ast` and ran it inside a hand-built namespace holding the three
globals it happened to need at the time. That namespace is a second copy
of the module's import list, and it went stale the moment the function
grew a fourth: `b04a517` split the apply ladder out into
`apply_diff_text` and added `drop_mode_only_sections`, and from then on
every test here died with `NameError` before reaching its assertion. The
file stayed green-looking because nothing ran it -- there is no CI -- so
a broken test and a passing one were indistinguishable for six commits.

So the methods are taken off the class instead. They are plain functions
until Python binds them, which means they can be bound to a stand-in
object carrying only the four attributes they actually read, with no
Docker and no image. Whatever `base.py` imports next is already in its
own module globals and resolves without anything here being touched.

What is faked is only the container boundary: `exec_run` shells out to
the same git and the same GNU patch a worker would use, in a scratch
repository, so the apply ladder is exercised rather than simulated.
"""

import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import sbmdt.evaluator.base as base

APPLY_PATCH = base.Evaluator.apply_patch
APPLY_DIFF_TEXT = base.Evaluator.apply_diff_text

# `PATCH_FILE` is an absolute container path. Joining it onto a Windows
# temp directory would escape that directory, so the tests rebind it to
# a relative name that lands beside the checkout.
PATCH_NAME = 'model.patch'

# Written as a code point because this file is authored through shell
# heredocs that collapse one level of escaping, and a mangled marker
# would make the test assert the wrong thing while still passing.
NO_NEWLINE = chr(92) + ' No newline at end of file'

HAVE_GNU_PATCH = shutil.which('patch') is not None


class FakeContainer:
    """Run the evaluator's commands in a scratch repository."""

    def __init__(self, root: Path):
        self.root = root
        self.commands: list[str] = []

    def exec_run(self, command, workdir=None, stream=False):
        # The evaluator passes a string for git and GNU patch and a list
        # for the `bash -c` blob materialisation. Both have to work, or
        # the gold path silently stops being covered.
        argv = command if isinstance(command, list) else command.split()
        self.commands.append(' '.join(argv))
        # Resolve through PATH as a shell would. On Windows a bare 'bash'
        # otherwise finds System32's WSL stub before Git's bash.
        argv = [shutil.which(argv[0]) or argv[0], *argv[1:]]
        done = subprocess.run(argv, cwd=self.root, capture_output=True)
        return done.returncode, done.stdout + done.stderr


class Harness:
    """A git repository holding `files`, plus a bound evaluator stub."""

    def __init__(self, files, instance_id='test', patch_type=''):
        self.files = files
        self.instance_id = instance_id
        self.patch_type = patch_type

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for args in (
            ('git', 'init', '-q'),
            ('git', 'config', 'core.autocrlf', 'false'),
            ('git', 'config', 'user.email', 'test@example.com'),
            ('git', 'config', 'user.name', 'test'),
        ):
            subprocess.run(args, cwd=self.root, check=True,
                           capture_output=True)
        for name, content in self.files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        for args in (('git', 'add', '-A'), ('git', 'commit', '-qm', 'base')):
            subprocess.run(args, cwd=self.root, check=True,
                           capture_output=True)

        self.container = FakeContainer(self.root)
        self.obj = types.SimpleNamespace(
            container=self.container,
            pred=types.SimpleNamespace(model_patch=None),
            instance_id=self.instance_id,
            patch_type=self.patch_type,
        )
        self.obj.apply_diff_text = types.MethodType(APPLY_DIFF_TEXT, self.obj)

        def write(container, file, content):
            (container.root / file).write_text(content, newline='')

        self._patches = [
            mock.patch.object(base, 'write_to_container', write),
            mock.patch.object(base, 'PATCH_FILE', PATCH_NAME),
        ]
        for patcher in self._patches:
            patcher.start()
        return self

    def __exit__(self, *exc):
        for patcher in self._patches:
            patcher.stop()
        self._tmp.cleanup()
        return False

    def apply(self, patch):
        self.obj.pred.model_patch = patch
        APPLY_PATCH(self.obj)

    def read(self, name):
        return (self.root / name).read_bytes()


def section(path, old, new, header=True):
    """One minimal single-hunk section for `path`."""
    head = f'diff --git a/{path} b/{path}\n' if header else ''
    return (
        head
        + f'--- a/{path}\n+++ b/{path}\n'
        + f'@@ -1 +1 @@\n-{old}\n+{new}\n'
    )


class PatchTransportTests(unittest.TestCase):
    """The original five, now against the code that actually ships."""

    def setUp(self):
        self.files = {'example.txt': b'old\n'}

    def test_missing_transport_newline(self):
        with Harness(self.files) as h:
            h.apply('--- a/example.txt\n+++ b/example.txt\n'
                    '@@ -1 +1 @@\n-old\n+new')
            self.assertEqual(h.read('example.txt'), b'new\n')

    def test_valid_patch(self):
        with Harness(self.files) as h:
            h.apply('--- a/example.txt\n+++ b/example.txt\n'
                    '@@ -1 +1 @@\n-old\n+new\n')
            self.assertEqual(h.read('example.txt'), b'new\n')

    def test_preserves_no_newline_marker(self):
        with Harness(self.files) as h:
            h.apply('--- a/example.txt\n+++ b/example.txt\n'
                    '@@ -1 +1 @@\n-old\n+new\n' + NO_NEWLINE)
            self.assertEqual(h.read('example.txt'), b'new')

    def test_rejects_mismatched_content(self):
        with Harness(self.files) as h:
            with self.assertRaisesRegex(Exception, 'Failed to apply patch'):
                h.apply('--- a/example.txt\n+++ b/example.txt\n'
                        '@@ -1 +1 @@\n-unrelated\n+new')

    def test_drops_binary_stub_without_data(self):
        with Harness(self.files) as h:
            h.apply(
                'diff --git a/image.png b/image.png\n'
                'index 1111111..2222222 100644\n'
                'Binary files a/image.png and b/image.png differ\n'
                + section('example.txt', 'old', 'new')
            )
            self.assertEqual(h.read('example.txt'), b'new\n')


class ModeOnlyTests(unittest.TestCase):
    """The behaviour that made 31 Refact submissions collapse to nothing.

    Those patches carry up to 1945 pure mode-change sections around a
    single real hunk. Dropping them is right -- an executable bit cannot
    change a test outcome -- but it is also what leaves one file behind,
    and a one-file patch skips the per-file retry below. Both halves are
    pinned here so neither can drift without the other being noticed.
    """

    def test_mode_only_sections_do_not_block_the_real_hunk(self):
        noise = ''.join(
            f'diff --git a/noise{i}.txt b/noise{i}.txt\n'
            'old mode 100644\nnew mode 100755\n'
            for i in range(50)
        )
        with Harness({'example.txt': b'old\n'}) as h:
            h.apply(noise + section('example.txt', 'old', 'new'))
            self.assertEqual(h.read('example.txt'), b'new\n')

    def test_a_mode_change_beside_a_hunk_is_kept(self):
        patch = (
            'diff --git a/example.txt b/example.txt\n'
            'old mode 100644\nnew mode 100755\n'
            '--- a/example.txt\n+++ b/example.txt\n'
            '@@ -1 +1 @@\n-old\n+new\n'
        )
        with Harness({'example.txt': b'old\n'}) as h:
            h.apply(patch)
            self.assertEqual(h.read('example.txt'), b'new\n')

    def test_an_all_mode_patch_applies_cleanly_and_changes_nothing(self):
        noise = (
            'diff --git a/example.txt b/example.txt\n'
            'old mode 100644\nnew mode 100755\n'
        )
        with Harness({'example.txt': b'old\n'}) as h:
            h.apply(noise)
            self.assertEqual(h.read('example.txt'), b'old\n')


class PerFileFallbackTests(unittest.TestCase):
    """One unplaceable file must not discard the rest of the submission.

    This is the `b04a517` behaviour and the reason `apply_diff_text`
    exists. It is worth its own test because the retry is gated on
    `len(per_file) > 1`: a two-file patch keeps the good half, a
    one-file patch has nothing to keep and raises, and those two
    outcomes are indistinguishable from the log alone.
    """

    def test_a_bad_lockfile_does_not_discard_the_code_change(self):
        files = {'example.txt': b'old\n', 'package-lock.json': b'real\n'}
        with Harness(files) as h:
            h.apply(section('example.txt', 'old', 'new')
                    + section('package-lock.json', 'imaginary', 'other'))
            self.assertEqual(h.read('example.txt'), b'new\n')
            self.assertEqual(h.read('package-lock.json'), b'real\n')

    def test_a_single_unplaceable_file_still_raises(self):
        with Harness({'example.txt': b'old\n'}) as h:
            with self.assertRaisesRegex(Exception, 'Failed to apply patch'):
                h.apply(section('example.txt', 'imaginary', 'new'))

    def test_every_file_refusing_raises_rather_than_passing_quietly(self):
        files = {'a.txt': b'real\n', 'b.txt': b'real\n'}
        with Harness(files) as h:
            with self.assertRaisesRegex(Exception, 'Failed to apply patch'):
                h.apply(section('a.txt', 'imaginary', 'x')
                        + section('b.txt', 'imaginary', 'y'))
            self.assertEqual(h.read('a.txt'), b'real\n')


@unittest.skipUnless(HAVE_GNU_PATCH, 'GNU patch not on PATH')
class GnuPatchFallbackTests(unittest.TestCase):
    """The last rung, ungated in `aa5a058`.

    `git apply --3way` needs the diff's pre-image blob in the object
    store. A patch written against a commit the image does not have
    fails all three git attempts, and before the gate was removed the
    instance was lost with no result at all. GNU patch matches on
    context instead, so it lands.

    Finding a fixture that actually reaches the last rung took two
    tries. Wrong line numbers do not: `--recount` is the second rung
    precisely to repair those, so git places the hunk and GNU patch is
    never called. What git has no answer for is *mismatched context*,
    because it applies no fuzz at all -- so one wrong context line
    fails plain, fails `--recount`, and fails `--3way` twice over
    (lacking the blob, then again on direct application), while GNU
    patch lands it at fuzz 1. The bogus `index` line is what denies
    `--3way` its blob, which is the real-world shape of this.
    """

    def test_context_matching_rescues_a_patch_git_refuses(self):
        body = b'alpha\none\ntwo\nthree\nomega\n'
        stale = (
            'diff --git a/example.txt b/example.txt\n'
            'index 1111111..2222222 100644\n'
            '--- a/example.txt\n+++ b/example.txt\n'
            '@@ -1,5 +1,5 @@\n DIFFERENT\n one\n-two\n+TWO\n three\n omega\n'
        )
        with Harness({'example.txt': body}) as h:
            h.apply(stale)
            self.assertEqual(h.read('example.txt'),
                             b'alpha\none\nTWO\nthree\nomega\n')
            self.assertTrue(
                any(c.startswith('patch ') for c in h.container.commands),
                'GNU patch was never reached',
            )

    def test_git_handles_what_it_can_without_reaching_patch(self):
        with Harness({'example.txt': b'old\n'}) as h:
            h.apply(section('example.txt', 'old', 'new'))
            self.assertFalse(
                any(c.startswith('patch ') for c in h.container.commands),
                'fell through to GNU patch for a patch git could place',
            )


class AlreadyAppliedTests(unittest.TestCase):
    """What the 2026-09-21 follow-up round actually hit.

    Refact's karma hunk repoints CHROME_BIN at the system Chrome, which
    the official image already does, so the post-image is present before
    the patch runs. GNU patch calls that "Reversed (or previously
    applied)" and refuses under --forward. It has to stay an error: a
    submission whose only content is already in the tree contributed
    nothing, and quietly returning success would score it as though the
    agent had fixed something.
    """

    @unittest.skipUnless(HAVE_GNU_PATCH, 'GNU patch not on PATH')
    def test_an_already_applied_hunk_is_not_reported_as_success(self):
        with Harness({'example.txt': b'new\n'}) as h:
            with self.assertRaisesRegex(Exception, 'Failed to apply patch'):
                h.apply(section('example.txt', 'old', 'new'))


CR = chr(13)
CODE = 'components/prism-sql.js'
TEST = 'tests/languages/sql/string_feature.test'
# A CRLF test file whose case holds a string spanning two lines, so the
# CR is part of what the test expects.
TEST_FILE = f"'foo{CR}\nbar'{CR}\n".encode()
TEST_PATCH = (
    f'diff --git a/{TEST} b/{TEST}\n--- a/{TEST}\n+++ b/{TEST}\n'
    f"@@ -1,2 +1,3 @@\n 'foo{CR}\n bar'{CR}\n+'foo''s bar'{CR}\n"
)


class GoldKeepsTheTestPatchBytes(unittest.TestCase):
    """prism-1500: the gold pred reached us with every CR removed."""

    def gold(self, test_patch):
        files = {CODE: b'old\n', TEST: TEST_FILE}
        pred = section(CODE, 'old', 'new') + TEST_PATCH.replace(CR, '')
        with Harness(files, patch_type='gold') as h, mock.patch.object(
                base, 'test_patch_for', lambda _: test_patch):
            h.apply(pred)
            return h.read(CODE), h.read(TEST)

    def test_the_test_file_keeps_its_crlf_bytes(self):
        code, test = self.gold(TEST_PATCH)
        self.assertEqual(code, b'new\n')
        self.assertEqual(test, TEST_FILE + f"'foo''s bar'{CR}\n".encode())

    def test_a_test_patch_that_is_not_the_same_diff_is_not_used(self):
        other = TEST_PATCH.replace("'foo''s bar'", "'something else'")
        _, test = self.gold(other)
        self.assertEqual(test, b"'foo\nbar'\n'foo''s bar'\n")


if __name__ == '__main__':
    unittest.main(verbosity=2)
