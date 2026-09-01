from __future__ import annotations

import datetime as dt
import logging

from sbmdt.evaluator.base import Evaluator, PatchType, TestResult
from sbmdt.pred import Pred

__all__ = ['evaluate']

log = logging.getLogger(__name__)


def _load_evaluator_cls(instance_id: str) -> type[Evaluator]:
    """Return the evaluator class for ``instance_id`` via lazy import.

    Each evaluator module is imported only when its prefix matches, so
    unrelated evaluator modules are never loaded. No prefix here is
    itself a prefix of another, so branch order does not affect which
    evaluator is selected.

    Args:
        instance_id: Identifier whose prefix selects the evaluator.

    Returns:
        The evaluator class matching ``instance_id``'s prefix.

    Raises:
        Exception: If ``instance_id`` does not match any known evaluator.
    """
    if instance_id.startswith('alibaba'):
        from sbmdt.evaluator.alibaba import AlibabaEvaluator

        return AlibabaEvaluator
    if instance_id.startswith('grommet'):
        from sbmdt.evaluator.grommet import GrommetEvaluator

        return GrommetEvaluator
    if instance_id.startswith('GoogleChrome'):
        from sbmdt.evaluator.lighthouse import LighthouseEvaluator

        return LighthouseEvaluator
    if instance_id.startswith('prettier'):
        from sbmdt.evaluator.prettier import PrettierEvaluator

        return PrettierEvaluator
    if instance_id.startswith('PrismJS'):
        from sbmdt.evaluator.prismjs import PrismjsEvaluator

        return PrismjsEvaluator
    if instance_id.startswith('carbon'):
        from sbmdt.evaluator.carbon import CarbonEvaluator

        return CarbonEvaluator
    if instance_id.startswith('quarto-dev'):
        from sbmdt.evaluator.quarto import QuartoEvaluator

        return QuartoEvaluator
    if instance_id.startswith('openlayers'):
        from sbmdt.evaluator.openlayers import OpenlayersEvaluator

        return OpenlayersEvaluator
    if instance_id.startswith('scratchfoundation'):
        from sbmdt.evaluator.scratchgui import ScratchGuiEvaluator

        return ScratchGuiEvaluator
    if instance_id.startswith('bpmn-io'):
        from sbmdt.evaluator.bpmn import BpmnEvaluator

        return BpmnEvaluator
    if instance_id.startswith('eslint'):
        from sbmdt.evaluator.eslint import ESLintEvaluator

        return ESLintEvaluator
    if instance_id.startswith('highlightjs'):
        from sbmdt.evaluator.highlightjs import HighlightjsEvaluator

        return HighlightjsEvaluator
    raise Exception(f'unknown instance ID {instance_id}')


def evaluate(
    instance_id: str,
    timestamp: dt.datetime,
    patch_type: PatchType,
    pred: Pred | None,
    apply_test_patch: bool = False,
) -> list[TestResult]:
    """Run a benchmark evaluation for a single instance.

    Selects the concrete evaluator for ``instance_id`` based on its
    prefix, then runs its full setup/apply_patch/evaluate/cleanup
    lifecycle.

    Args:
        instance_id: Identifier of the benchmark instance to evaluate.
            Its prefix (e.g. ``'alibaba'``) determines which evaluator
            handles it.
        timestamp: The timestamp of the start of the run.
        patch_type: The patch state to run under. When this is anything
            other than :attr:`PatchType.BEFORE_PATCH`, ``pred``'s patch
            is applied before the test suite runs.
        pred: The model-generated patch to apply, or ``None`` when
            ``patch_type`` is :attr:`PatchType.BEFORE_PATCH`.
        apply_test_patch: Whether to apply the instance's
            ``test_patch.diff`` on top of ``pred``, so the maintainer's
            FAIL_TO_PASS tests are present regardless of what the model
            wrote. Requires ``scripts/split_gold_patch.py`` to have run.

    Returns:
        A list of :class:`TestResult` from the evaluation run.

    Raises:
        Exception: If ``instance_id`` does not match any known evaluator.
    """
    log.info(
        f'Evaluating instance {instance_id} {patch_type} from '
        f'{Pred.get_agent_name(pred)} at {timestamp.isoformat()}'
    )

    log.info(f'Loading evaluator module for instance {instance_id}')
    evaluator_cls = _load_evaluator_cls(instance_id)
    log.info(f'Loaded evaluator {evaluator_cls.__name__}')

    evaluator = evaluator_cls(
        instance_id=instance_id,
        timestamp=timestamp,
        patch_type=patch_type,
        agent_name=Pred.get_agent_name(pred),
        pred=pred,
        apply_test_patch=apply_test_patch,
    )

    log.info(f'Running evaluation with {evaluator_cls.__name__}')
    return evaluator.run()
