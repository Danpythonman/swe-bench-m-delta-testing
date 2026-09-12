"""Read-only S3 audit; validate missing patches using git's diff parser."""

import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

import boto3


def identity(key, prediction=False):
    text = unquote(key)
    pattern = (
        r'^(.*?)_(with_image|without_image)_(.*?)_'
        r'\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}Z\.pred$'
        if prediction
        else r'^(.*?)-(with_image|without_image)-(.*?)-'
        r'\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}Z$'
    )
    match = re.match(pattern, text)
    return match.groups() if match else None


def syntax(patch):
    result = subprocess.run(
        ['git', 'apply', '--numstat', '-'],
        input=patch.encode(),
        capture_output=True,
        check=False,
    )
    return result.returncode == 0, result.stderr.decode(
        errors='replace'
    ).strip()


def main():
    s3 = boto3.Session(profile_name='default').client('s3')

    def keys(bucket):
        return [
            obj['Key']
            for page in s3.get_paginator('list_objects_v2').paginate(
                Bucket=bucket
            )
            for obj in page.get('Contents', [])
        ]

    predictions = {
        identity(k, True): k for k in keys('sbmdt-preds') if identity(k, True)
    }
    results = {identity(k) for k in keys('sbmdt-test-results') if identity(k)}
    pairs = {r[:2] for r in results}
    rows = []
    for ident, key in sorted(predictions.items()):
        row = dict(
            instance=ident[0], variant=ident[1], model=ident[2], key=key
        )
        row['result_present'] = ident in results
        row['pair_result_present'] = ident[:2] in pairs
        if ident not in results:
            pred = json.loads(
                s3.get_object(Bucket='sbmdt-preds', Key=key)['Body'].read()
            )
            patch = pred['model_patch']
            valid, error = syntax(patch)
            normalized, normalized_error = syntax(
                patch + '\n' if patch and not patch.endswith('\n') else patch
            )
            row.update(
                syntax_valid=valid,
                syntax_error=error,
                newline_repair_valid=normalized,
                remaining_error=normalized_error,
            )
        rows.append(row)
    report = dict(
        total=len(rows),
        exact_model_results=sum(r['result_present'] for r in rows),
        pair_matched_results=sum(r['pair_result_present'] for r in rows),
        newline_repair_candidates=sum(
            not r.get('syntax_valid', True)
            and r.get('newline_repair_valid', False)
            for r in rows
        ),
        rows=rows,
    )
    Path('coverage_audit.json').write_text(
        json.dumps(report, indent=2), encoding='utf-8'
    )
    print(
        json.dumps({k: v for k, v in report.items() if k != 'rows'}, indent=2)
    )
    print(
        'Missing by repository:',
        dict(
            Counter(
                r['instance'].split('__')[0]
                for r in rows
                if not r['result_present']
            )
        ),
    )


if __name__ == '__main__':
    main()
