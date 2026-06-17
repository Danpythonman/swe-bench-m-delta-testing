import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from sbmdt import evaluate
from sbmdt.env import PROJECT_BASE
from sbmdt.evaluator.base import PatchType
from sbmdt.log import setup_logging
from sbmdt.pred import Pred


@dataclass(kw_only=True)
class Args:
    instance_id: str
    log_file: Path
    patch_type: PatchType
    pred: Pred | None


def parse_args() -> Args:
    parser = argparse.ArgumentParser(
        description='Run delta testing evaluation for a benchmark instance.'
    )

    parser.add_argument(
        'instance_id',
        help=(
            'Benchmark instance ID to evaluate, e.g. '
            "'alibaba-fusion__next-717'."
        ),
    )
    parser.add_argument(
        'patch_type',
        type=PatchType,
        choices=list(PatchType),
        help='Patch state under which the test was executed.',
    )
    parser.add_argument(
        '--pred-file',
        type=Path,
        default=None,
        help=(
            'Path to .pred file (required unless patch_type is before_patch).'
        ),
    )
    parser.add_argument(
        '--log-file',
        type=Path,
        default=PROJECT_BASE / 'logs' / 'log.log',
        help='Path to write logs to (default: logs/log.log).',
    )

    ns = parser.parse_args()

    if ns.patch_type == PatchType.BEFORE_PATCH and ns.pred_file is not None:
        parser.error(
            '--pred-file should not be provided when patch_type is '
            'before_patch.'
        )
    if ns.patch_type != PatchType.BEFORE_PATCH and ns.pred_file is None:
        parser.error(
            '--pred-file is required when patch_type is not before_patch.'
        )

    pred: Pred | None = None
    if ns.pred_file is not None:
        if not ns.pred_file.exists():
            parser.error(f'--pred-file does not exist: {ns.pred_file}')
        pred = Pred.from_file(ns.pred_file)

    return Args(
        instance_id=ns.instance_id,
        log_file=ns.log_file,
        patch_type=ns.patch_type,
        pred=pred,
    )


def main() -> None:
    args = parse_args()

    log_path = Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    setup_logging(log_file=log_path)

    results_dir = PROJECT_BASE / 'results'
    results_dir.mkdir(exist_ok=True)
    filename = (
        f'{args.instance_id}-{args.patch_type}-'
        f'{Pred.get_agent_name(args.pred)}.json'
    )
    results_path = results_dir / filename
    if results_path.exists():
        raise Exception('results already exist')

    results = evaluate(args.instance_id, args.patch_type, args.pred)

    with open(results_dir / filename, 'w') as f:
        json.dump([asdict(r) for r in results], f, indent=4)


if __name__ == '__main__':
    main()
