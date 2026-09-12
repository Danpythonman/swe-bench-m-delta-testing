"""Exercise the evaluator method against git in temporary repositories."""

import ast
import logging
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sbmdt.patches import drop_unappliable_binary

BASE = Path(__file__).resolve().parents[1] / 'src/sbmdt/evaluator/base.py'
tree = ast.parse(BASE.read_text(encoding='utf-8'))
method = next(
    node
    for cls in tree.body
    if isinstance(cls, ast.ClassDef)
    for node in cls.body
    if isinstance(node, ast.FunctionDef) and node.name == 'apply_patch'
)
namespace = dict(
    log=logging.getLogger(__name__),
    PATCH_FILE='input.diff',
    drop_unappliable_binary=drop_unappliable_binary,
)
exec(
    compile(ast.Module(body=[method], type_ignores=[]), str(BASE), 'exec'),
    namespace,
)


class PatchTransportTests(unittest.TestCase):
    def run_patch(self, patch):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', directory], check=True)
            subprocess.run(
                ['git', '-C', directory, 'config', 'core.autocrlf', 'false'],
                check=True,
            )
            (root / 'example.txt').write_bytes(b'old\n')

            def write(container, filename, content):
                (root / filename).write_bytes(content.encode())

            def execute(command, **kwargs):
                result = subprocess.run(
                    command.split(), cwd=root, capture_output=True
                )
                return result.returncode, result.stdout + result.stderr

            namespace['write_to_container'] = write
            obj = SimpleNamespace(
                container=SimpleNamespace(exec_run=execute),
                pred=SimpleNamespace(model_patch=patch),
                instance_id='test',
            )
            namespace['apply_patch'](obj)
            return (root / 'example.txt').read_bytes()

    def test_missing_transport_newline(self):
        self.assertEqual(
            self.run_patch(
                '--- a/example.txt\n+++ b/example.txt\n@@ -1 +1 @@\n-old\n+new'
            ),
            b'new\n',
        )

    def test_valid_patch(self):
        self.assertEqual(
            self.run_patch(
                '--- a/example.txt\n+++ b/example.txt\n'
                '@@ -1 +1 @@\n-old\n+new\n'
            ),
            b'new\n',
        )

    def test_preserves_no_newline_marker(self):
        self.assertEqual(
            self.run_patch(
                '--- a/example.txt\n+++ b/example.txt\n'
                '@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file'
            ),
            b'new',
        )

    def test_rejects_mismatched_content(self):
        with self.assertRaisesRegex(Exception, 'Failed to apply patch'):
            self.run_patch(
                '--- a/example.txt\n+++ b/example.txt\n'
                '@@ -1 +1 @@\n-unrelated\n+new'
            )

    def test_drops_binary_stub_without_data(self):
        patch = (
            'diff --git a/image.png b/image.png\n'
            'index 1111111..2222222 100644\n'
            'Binary files a/image.png and b/image.png differ\n'
            'diff --git a/example.txt b/example.txt\n'
            '--- a/example.txt\n+++ b/example.txt\n'
            '@@ -1 +1 @@\n-old\n+new\n'
        )
        self.assertEqual(self.run_patch(patch), b'new\n')


if __name__ == '__main__':
    unittest.main()
