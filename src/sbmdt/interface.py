"""
Public entrypoint for running a benchmark evaluation.

Exposes a single ``evaluate`` function that picks the right concrete
:class:`~sbmdt.evaluator.base.Evaluator` for a given instance ID and runs
it. Callers (CLI scripts, notebooks, etc.) should go through this module
rather than constructing evaluators directly.
"""

from __future__ import annotations

import datetime as dt
import logging

from sbmdt.evaluator.alibaba import AlibabaEvaluator
from sbmdt.evaluator.eslint import ESLintEvaluator
from sbmdt.evaluator.base import PatchType, TestResult
from sbmdt.evaluator.bpmn import BpmnEvaluator
from sbmdt.evaluator.grommet import GrommetEvaluator
from sbmdt.evaluator.lighthouse import LighthouseEvaluator
from sbmdt.evaluator.openlayers import OpenlayersEvaluator
from sbmdt.evaluator.prettier import PrettierEvaluator
from sbmdt.evaluator.scratchgui import ScratchGuiEvaluator
from sbmdt.pred import Pred

__all__ = ['evaluate']

log = logging.getLogger(__name__)


def evaluate(
    instance_id: str,
    timestamp: dt.datetime,
    patch_type: PatchType,
    pred: Pred | None,
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

    Returns:
        A list of :class:`TestResult` from the evaluation run.

    Raises:
        Exception: If ``instance_id`` does not match any known evaluator.
    """
    log.info(
        f'Evaluating instance {instance_id} {patch_type} from '
        f'{Pred.get_agent_name(pred)} at {timestamp.isoformat()}'
    )
    if instance_id.startswith('alibaba'):
        evaluator = AlibabaEvaluator(
            instance_id=instance_id,
            timestamp=timestamp,
            patch_type=patch_type,
            agent_name=Pred.get_agent_name(pred),
            pred=pred,
        )
    elif instance_id.startswith('grommet'):
        evaluator = GrommetEvaluator(
            instance_id=instance_id,
            timestamp=timestamp,
            patch_type=patch_type,
            agent_name=Pred.get_agent_name(pred),
            pred=pred,
        )
    elif instance_id.startswith('GoogleChrome'):
        evaluator = LighthouseEvaluator(
            instance_id=instance_id,
            patch_type=patch_type,
            agent_name=Pred.get_agent_name(pred),
            pred=pred,
        )
    elif instance_id.startswith('prettier'):
        evaluator = PrettierEvaluator(
            instance_id=instance_id,
            patch_type=patch_type,
            agent_name=Pred.get_agent_name(pred),
            pred=pred,
        )
    elif instance_id.startswith('openlayers'):
        evaluator = OpenlayersEvaluator(
            instance_id=instance_id,
            patch_type=patch_type,
            agent_name=Pred.get_agent_name(pred),
            pred=pred,
        )
    elif instance_id.startswith('scratchfoundation'):
        evaluator = ScratchGuiEvaluator(
            instance_id=instance_id,
            patch_type=patch_type,
            agent_name=Pred.get_agent_name(pred),
            pred=pred,
        )

    elif instance_id.startswith('bpmn-io'):
        evaluator = BpmnEvaluator(
            instance_id=instance_id,
            patch_type=patch_type,
            agent_name=Pred.get_agent_name(pred),
            pred=pred,
        )
    elif instance_id.startswith('eslint'):
        evaluator = ESLintEvaluator(
            instance_id=instance_id,
            patch_type=patch_type,
            agent_name=Pred.get_agent_name(pred),
            pred=pred,
        )
    else:
        raise Exception(f'unknown instance ID {instance_id}')

    return evaluator.run()
