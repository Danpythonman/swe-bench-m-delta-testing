"""Unit checks for result provenance and the launch-commit guards."""

import datetime as dt
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

import sbmdt.provenance as provenance
from sbmdt.evaluator.base import PatchType, TestResult
from sbmdt.parquet import read_test_results, test_results_to_parquet_table
from scripts import run_all_ec2

COMMIT = 'a' * 40
RESULT = TestResult(
    instance_id='eslint__eslint-9436',
    patch_type=PatchType.GOLD,
    agent_name='GOLD',
    timestamp=dt.datetime(2026, 11, 2, tzinfo=dt.UTC),
    test_name='no-unused-vars valid',
    passed=True,
    harness_commit=COMMIT,
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ['git', '-C', str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class HarnessCommitTests(unittest.TestCase):
    def setUp(self):
        provenance.harness_commit.cache_clear()
        self.addCleanup(provenance.harness_commit.cache_clear)

    def test_environment_wins(self):
        with patch.dict(os.environ, {'SBMDT_COMMIT': COMMIT}):
            self.assertEqual(provenance.harness_commit(), COMMIT)

    def test_dirty_only_for_tracked_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, 'init', '-q')
            git(root, 'config', 'user.email', 'test@example.com')
            git(root, 'config', 'user.name', 'test')
            (root / 'src').mkdir()
            (root / 'src' / 'a.py').write_text('x = 1\n')
            (root / 'notes.md').write_text('notes\n')
            git(root, 'add', '-A')
            git(root, 'commit', '-qm', 'base')
            head = git(root, 'rev-parse', 'HEAD')
            with (
                patch.object(provenance, 'PROJECT_BASE', root),
                patch.dict(os.environ, {}, clear=True),
            ):
                (root / 'notes.md').write_text('edited\n')
                self.assertEqual(provenance.harness_commit(), head)
                provenance.harness_commit.cache_clear()
                (root / 'src' / 'a.py').write_text('x = 2\n')
                self.assertEqual(provenance.harness_commit(), f'{head}-dirty')

    def test_outside_a_checkout(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(provenance, 'PROJECT_BASE', Path(directory)),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertIsNone(provenance.harness_commit())


class ResultCommitTests(unittest.TestCase):
    def test_dict_round_trip(self):
        self.assertEqual(
            TestResult.from_dict(RESULT.to_dict(json_safe=True)), RESULT
        )

    def test_old_results_read_the_commit_as_null(self):
        with tempfile.TemporaryDirectory() as directory:
            old = pa.table(
                {
                    'instance_id': ['x'],
                    'patch_type': ['gold'],
                    'agent_name': ['GOLD'],
                    'timestamp': pa.array(
                        [dt.datetime(2026, 9, 20, tzinfo=dt.UTC)],
                        pa.timestamp('us', tz='UTC'),
                    ),
                    'test_name': ['t'],
                    'passed': [True],
                }
            )
            pq.write_table(old, Path(directory) / 'old.parquet')
            pq.write_table(
                test_results_to_parquet_table([RESULT]),
                Path(directory) / 'new.parquet',
            )
            frame = read_test_results(Path(directory))
            commits = dict(
                zip(frame.test_name, frame.harness_commit, strict=True)
            )
            self.assertTrue(pd.isna(commits['t']))
            self.assertEqual(commits[RESULT.test_name], COMMIT)


class CheckoutCommandTests(unittest.TestCase):
    def test_pinned_commit(self):
        command = run_all_ec2.make_git_checkout_command('main', COMMIT)
        self.assertIn(f'fetch --depth 1 origin {COMMIT}', command)
        self.assertIn(f'checkout -B main {COMMIT}', command)

    def test_branch_tip_without_commit(self):
        command = run_all_ec2.make_git_checkout_command('main')
        self.assertIn('checkout -B main origin/main', command)


if __name__ == '__main__':
    unittest.main()
