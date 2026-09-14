"""Verify patches are applied before setup mutates the checkout."""

import ast
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import final

from sbmdt.evaluator.base import MODEL_PATCH_TYPES, PatchType, TestResult

BASE = Path(__file__).resolve().parents[1] / 'src/sbmdt/evaluator/base.py'
tree = ast.parse(BASE.read_text(encoding='utf-8'))
method = next(
    node
    for cls in tree.body
    if isinstance(cls, ast.ClassDef)
    for node in cls.body
    if isinstance(node, ast.FunctionDef) and node.name == 'run'
)
namespace = dict(
    log=logging.getLogger(__name__),
    PatchType=PatchType,
    TestResult=TestResult,
    MODEL_PATCH_TYPES=MODEL_PATCH_TYPES,
    final=final,
)
exec(
    compile(ast.Module(body=[method], type_ignores=[]), str(BASE), 'exec'),
    namespace,
)


class EvaluatorLifecycleTests(unittest.TestCase):
    def run_lifecycle(self, patch_type, apply_test_patch=True, fail=None):
        stages = []

        def stage(name, result=None):
            def call():
                stages.append(name)
                if name == fail:
                    raise RuntimeError(name)
                return result

            return call

        obj = SimpleNamespace(
            instance_id='test-instance',
            patch_type=patch_type,
            apply_test_patch_enabled=apply_test_patch,
            provision=stage('provision'),
            apply_patch=stage('apply_patch'),
            apply_test_patch=stage('apply_test_patch'),
            setup=stage('setup'),
            evaluate=stage('evaluate', ['ok']),
            cleanup=stage('cleanup'),
        )
        if fail:
            with self.assertRaisesRegex(RuntimeError, fail):
                namespace['run'](obj)
        else:
            self.assertEqual(namespace['run'](obj), ['ok'])
        return stages

    def test_model_patch_and_tests_precede_setup(self):
        self.assertEqual(
            self.run_lifecycle(PatchType.WITH_IMAGE),
            [
                'provision',
                'apply_patch',
                'apply_test_patch',
                'setup',
                'evaluate',
                'cleanup',
            ],
        )

    def test_gold_patch_precedes_setup_without_separate_test_patch(self):
        self.assertEqual(
            self.run_lifecycle(PatchType.GOLD),
            ['provision', 'apply_patch', 'setup', 'evaluate', 'cleanup'],
        )

    def test_before_patch_applies_only_test_patch(self):
        self.assertEqual(
            self.run_lifecycle(PatchType.BEFORE_PATCH),
            [
                'provision',
                'apply_test_patch',
                'setup',
                'evaluate',
                'cleanup',
            ],
        )

    def test_cleanup_still_runs_after_failure(self):
        self.assertEqual(
            self.run_lifecycle(PatchType.WITHOUT_IMAGE, fail='setup'),
            [
                'provision',
                'apply_patch',
                'apply_test_patch',
                'setup',
                'cleanup',
            ],
        )


if __name__ == '__main__':
    unittest.main()
