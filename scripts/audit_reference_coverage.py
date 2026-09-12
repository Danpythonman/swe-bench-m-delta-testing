"""Find evaluated model instances that lack before-patch or gold results."""

import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

import boto3


def parse_prediction(key):
    match = re.match(
        r'^(.*?)_(before_patch|gold|with_image|without_image)_(.*?)\.pred$',
        unquote(key),
    )
    return match.groups() if match else None


def parse_result(key):
    match = re.match(
        r'^(.*?)-(before_patch|gold|with_image|without_image)-(.*?)-'
        r'\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}Z$',
        unquote(key),
    )
    return match.groups() if match else None


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

    predictions = {}
    for key in keys('sbmdt-preds'):
        parsed = parse_prediction(key)
        if parsed:
            predictions[(parsed[0], parsed[1])] = key
    results = {
        (parsed[0], parsed[1])
        for key in keys('sbmdt-test-results')
        if (parsed := parse_result(key))
    }
    evaluated = {
        instance
        for instance, patch_type in results
        if patch_type in ('with_image', 'without_image')
    }
    rows = []
    for instance in sorted(evaluated):
        missing = [
            patch_type
            for patch_type in ('before_patch', 'gold')
            if (instance, patch_type) not in results
        ]
        for patch_type in missing:
            rows.append(
                {
                    'instance': instance,
                    'patch_type': patch_type,
                    'prediction_key': predictions.get((instance, patch_type)),
                }
            )
    report = {
        'evaluated_instances': len(evaluated),
        'unscored_instances': len({row['instance'] for row in rows}),
        'missing_reference_runs': len(rows),
        'missing_prediction_files': sum(
            row['prediction_key'] is None for row in rows
        ),
        'by_repository': dict(
            Counter(row['instance'].split('__', 1)[0] for row in rows)
        ),
        'rows': rows,
    }
    Path('reference_coverage_audit.json').write_text(
        json.dumps(report, indent=2), encoding='utf-8'
    )
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != 'rows'},
            indent=2,
        )
    )


if __name__ == '__main__':
    main()
