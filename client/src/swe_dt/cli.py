"""``swe-dt``: submit patches to the hosted SWE-DT API from the shell.

swe-dt instances
swe-dt submit fix.diff --instance django__django-11099 --wait
swe-dt submit preds.jsonl --wait      # SWE-bench predictions
swe-dt status <id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from swe_dt.client import ApiError, Client


def predictions(path: Path) -> list[dict[str, Any]]:
    """SWE-bench predictions in a ``.pred``/``.json`` file (one object or a
    list) or a ``.jsonl`` file (one object per line), each with
    ``instance_id`` and ``model_patch``.
    """
    text = path.read_text()
    rows: list[dict[str, Any]]
    if path.suffix == '.jsonl':
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        data: Any = json.loads(text)
        rows = list(data) if isinstance(data, list) else [data]  # pyright: ignore[reportUnknownArgumentType]
    for row in rows:
        if 'instance_id' not in row or 'model_patch' not in row:
            raise SystemExit(
                f'{path}: each prediction needs instance_id and model_patch'
            )
    return rows


def summary(evaluation: dict[str, Any]) -> str:
    head = (
        f'{evaluation["instance_id"]}  {evaluation.get("agent_name", "")}'
        f'  {evaluation["status"]}'
    )
    result = evaluation.get('result')
    if not result:
        error = evaluation.get('error')
        return head + (f': {error}' if error else '')
    verdict = 'RESOLVED' if result['resolved'] else 'UNRESOLVED'
    lines = [
        f'{head}  {verdict}  F2P {result["f2p_passed"]}/{result["f2p"]} '
        f'passed, P2P {result["p2p_lost"]} of {result["p2p"]} lost'
    ]
    lines += [f'    F2P failed  {t}' for t in result.get('failed_f2p', [])]
    lines += [f'    P2P lost    {t}' for t in result.get('lost_p2p', [])]
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='swe-dt', description='Submit patches to the hosted SWE-DT API.'
    )
    parser.add_argument('--api-key', help='Default: $SWE_DT_API_KEY.')
    parser.add_argument(
        '--url', help='Default: $SWE_DT_API_URL, then the public deployment.'
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='Print raw JSON instead of a summary.',
    )
    commands = parser.add_subparsers(dest='command', required=True)

    commands.add_parser(
        'instances', help='List instances that can be evaluated.'
    )

    submit = commands.add_parser('submit', help='Submit patches.')
    submit.add_argument(
        'file',
        type=Path,
        help='A unified diff (with '
        '--instance), or SWE-bench predictions (.pred, '
        '.json or .jsonl).',
    )
    submit.add_argument('--instance', help='Instance id for a plain diff.')
    submit.add_argument(
        '--agent-name', help="Default: the predictions' model_name_or_path."
    )
    submit.add_argument(
        '--wait', action='store_true', help='Wait for the verdicts.'
    )

    status = commands.add_parser('status', help='Show evaluations.')
    status.add_argument('ids', nargs='+')
    status.add_argument(
        '--wait', action='store_true', help='Wait until they finish.'
    )

    args = parser.parse_args(argv)
    try:
        client = Client(args.api_key, args.url)
    except ValueError as error:
        parser.error(str(error))

    def show(evaluation: dict[str, Any]) -> None:
        print(
            json.dumps(evaluation) if args.json else summary(evaluation),
            flush=True,
        )

    try:
        if args.command == 'instances':
            for instance in client.instances():
                print(instance)
            return 0

        if args.command == 'status':
            for id_ in args.ids:
                show(client.wait(id_) if args.wait else client.status(id_))
            return 0

        if args.instance:
            rows = [
                {
                    'instance_id': args.instance,
                    'model_patch': args.file.read_text(),
                }
            ]
        elif args.file.suffix in ('.pred', '.json', '.jsonl'):
            rows = predictions(args.file)
        else:
            parser.error('a plain diff needs --instance')
        jobs: list[dict[str, Any]] = []
        for row in rows:
            agent = args.agent_name or row.get('model_name_or_path')
            job = client.submit(
                row['instance_id'],
                row['model_patch'],
                agent,
                retry_busy=3600 if args.wait else 0,
            )
            jobs.append(job)
            if args.json:
                show(job)
            else:
                print(
                    f'{job["id"]}  {job["instance_id"]}  {job["status"]}',
                    file=sys.stderr if args.wait else sys.stdout,
                    flush=True,
                )
        if args.wait:
            for job in jobs:
                show(client.wait(job['id']))
        return 0
    except ApiError as error:
        print(f'swe-dt: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
