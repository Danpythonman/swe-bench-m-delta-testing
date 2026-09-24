"""openlayers rendering cases: their images are restored and they are run."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from sbmdt.evaluator.openlayers.openlayers import rendering_cases
from sbmdt.patches import git_blob_id, test_assets_for

_spec = importlib.util.spec_from_file_location(
    'test_split',
    os.path.join(os.path.dirname(__file__), '..', 'notebooks',
                 'test_split.py'))
ts = importlib.util.module_from_spec(_spec)
sys.modules['test_split'] = ts
_spec.loader.exec_module(ts)


class BlobId(unittest.TestCase):

    def test_matches_git(self):
        data = b'\x89PNG\r\n\x1a\n\x00binary'
        git = subprocess.run(['git', 'hash-object', '--stdin'], input=data,
                             capture_output=True, check=True)
        self.assertEqual(git_blob_id(data), git.stdout.decode().strip())


class TestAssets(unittest.TestCase):

    def write(self, base, manifest, files):
        assets = Path(base) / 'inst' / 'test_assets'
        assets.mkdir(parents=True)
        (assets / 'manifest.json').write_text(json.dumps(manifest))
        for name, data in files.items():
            (assets / name).write_bytes(data)

    def test_restores_matching_bytes_and_deletes(self):
        png = b'expected image'
        with tempfile.TemporaryDirectory() as base:
            self.write(base, {
                'test/rendering/cases/a/expected.png':
                    {'action': 'add', 'blob': git_blob_id(png)},
                'test/rendering/cases/old/expected.png': {'action': 'delete'},
            }, {git_blob_id(png): png})
            self.assertEqual(test_assets_for('inst', Path(base)), [
                ('test/rendering/cases/a/expected.png', png),
                ('test/rendering/cases/old/expected.png', None),
            ])

    def test_bytes_that_do_not_hash_to_the_blob_are_not_used(self):
        with tempfile.TemporaryDirectory() as base:
            blob = git_blob_id(b'the real image')
            self.write(base, {'x.png': {'action': 'add', 'blob': blob}},
                       {blob: b'something else'})
            self.assertEqual(test_assets_for('inst', Path(base)), [])

    def test_no_manifest_means_no_assets(self):
        with tempfile.TemporaryDirectory() as base:
            self.assertEqual(test_assets_for('inst', Path(base)), [])


class RenderingCases(unittest.TestCase):

    def test_case_directories_are_found_once(self):
        self.assertEqual(rendering_cases([
            'test/rendering/cases/webgl-tile/main.js',
            'test/rendering/cases/webgl-tile/expected.png',
            'test/rendering/cases/text-offset/main.js',
            'test/rendering/data/sprites/x.png',
            'test/browser/spec/ol/Map.test.js',
        ]), ['text-offset', 'webgl-tile'])

    def test_a_rendering_case_anchors_like_a_test_title(self):
        diff = ('diff --git a/test/rendering/cases/icon-sprite/main.js '
                'b/test/rendering/cases/icon-sprite/main.js\n'
                "+it('renders an icon sprite', () => {});\n")
        leaf, _ = ts.test_patch_titles(diff)
        self.assertEqual(leaf, ['renders an icon sprite',
                                'rendering icon-sprite'])


ADDS_A_RENDERING_CASE = (
    'diff --git a/test/rendering/cases/icon-sprite/main.js '
    'b/test/rendering/cases/icon-sprite/main.js\n'
    '+render();\n'
)


class RenderingCaseAsReference(unittest.TestCase):

    def split(self, gold_renders):
        rows = pd.DataFrame(
            [{'instance_id': 'demo', 'patch_type': p, 'agent_name': p,
              'timestamp': p, 'test_name': t, 'passed': ok}
             for p, t, ok in [
                 ('before_patch', 'rendering icon-sprite', False),
                 ('before_patch', 'Map renders', False),
                 ('gold', 'rendering icon-sprite', gold_renders),
                 ('gold', 'Map renders', True),
             ]])
        return ts.classify_tests(
            rows, pre_label='before_patch', post_label='gold',
            columns=ts.Columns(run='timestamp'),
            test_patch_diffs={'demo': ADDS_A_RENDERING_CASE})

    def test_the_case_is_the_fail_to_pass_test(self):
        self.assertEqual(self.split(gold_renders=True).fail_to_pass['demo'],
                         ['rendering icon-sprite'])

    def test_a_gold_pixel_mismatch_does_not_quarantine_the_instance(self):
        self.assertEqual(self.split(gold_renders=False).fail_to_pass['demo'],
                         ['Map renders'])


if __name__ == '__main__':
    unittest.main()
