"""Build the verified per-prediction benchmark status PDF."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

# ReportLab is supplied by the bundled document runtime. Append it after
# importing this project's compiled data stack so Python 3.12 NumPy binaries
# cannot shadow the project's Python 3.13 environment.
sys.path.append(
    r'C:\Users\parsa\.cache\codex-runtimes\codex-primary-runtime'
    r'\dependencies\python\Lib\site-packages'
)
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


def load_analysis(project: Path):
    path = project / 'scripts' / 'analyze_results.py'
    spec = importlib.util.spec_from_file_location('analysis_live', path)
    if spec is None or spec.loader is None:
        raise ImportError(f'Cannot load {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_parquets(directory: Path) -> pd.DataFrame:
    frames = []
    for path in directory.rglob('*'):
        if not path.is_file():
            continue
        try:
            frames.append(pq.read_table(path).to_pandas())
        except Exception:
            continue
    if not frames:
        raise ValueError(f'No readable result files under {directory}')
    return pd.concat(frames, ignore_index=True)


def failure_reason(text: str) -> str:
    lower = text.lower()
    checks = [
        ('post-rewrite esm', 'Unsupported Lighthouse ESM/yarn toolchain.'),
        (
            'neither "install-cli"',
            'Lighthouse install scripts are unavailable.',
        ),
        ('failed to install lighthouse-cli', 'Lighthouse CLI build failed.'),
        ('corrupt patch', 'Model patch was malformed.'),
        ('failed to apply test patch', 'Reference test patch did not apply.'),
        (
            'failed to apply patch',
            'Model patch did not apply to the base revision.',
        ),
        ('no results.xml', 'Test runner produced no JUnit results file.'),
        (
            'wrote no results.xml',
            'Test runner produced no JUnit results file.',
        ),
        ('timeout', 'Browser or test command timed out.'),
        ('pull rate limit', 'Container image pull was rate-limited.'),
        ('toomanyrequests', 'Container image pull was rate-limited.'),
        ('permission denied', 'Test executable lacked execute permission.'),
        (
            'cannot apply binary patch',
            'Patch contains a binary stub without data.',
        ),
        ('err_require_esm', 'Dependency module format is incompatible.'),
        ('cannot find module', 'A required test dependency is missing.'),
    ]
    for needle, reason in checks:
        if needle in lower:
            return reason
    exceptions = re.findall(r'Exception: ([^\n]+)', text)
    if exceptions:
        return exceptions[-1][:125]
    return 'Evaluation ended without an uploaded result.'


def load_status_details(root: Path):
    details = {}
    for folder in ('repair-runs', 'paired-runs', 'reference-runs'):
        for path in (root / folder).glob('*.json'):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except Exception:
                continue
            key = data.get('prediction_key', '')
            match = re.match(
                r'^(.*?)_(with_image|without_image|before_patch|gold)_',
                __import__('urllib.parse').parse.unquote(key),
            )
            if not match:
                continue
            text = '\n'.join(
                [
                    data.get('stdout', '') or '',
                    data.get('stderr', '') or '',
                    data.get('error', '') or '',
                ]
            )
            if data.get('status') == 'running':
                reason = 'Evaluation rerun is in progress.'
                patch_state = 'checking'
            elif data.get('status') == 'Success':
                reason = 'Evaluation command completed.'
                patch_state = 'applied'
            else:
                reason = failure_reason(text)
                if 'failed to apply patch for' in text.lower():
                    patch_state = 'failed'
                elif 'applying patch...' in text.lower():
                    patch_state = 'applied'
                else:
                    patch_state = 'not checked'
            details[(match.group(1), match.group(2))] = {
                'reason': reason,
                'patch': patch_state,
            }
    return details


def latest_run(frame: pd.DataFrame, instance: str, variant: str, agent: str):
    subset = frame[
        (frame['instance_id'] == instance)
        & (frame['patch_type'] == variant)
        & (frame['agent_name'] == agent)
    ]
    if subset.empty:
        return subset
    return subset[subset['timestamp'] == subset['timestamp'].max()]


def local_pair_identities(root: Path):
    folders = {
        'with_image': root.parent / 'with-images-recovered',
        'without_image': root.parent / 'without-images',
    }
    files = {
        variant: {path.stem: path for path in folder.glob('*.pred')}
        for variant, folder in folders.items()
    }
    paired = set(files['with_image']) & set(files['without_image'])
    expected = set()
    for variant, mapping in files.items():
        for instance in paired:
            pred = json.loads(mapping[instance].read_text(encoding='utf-8'))
            expected.add((instance, variant, pred['model_name_or_path']))
    unmatched = sorted(set(files['with_image']) ^ set(files['without_image']))
    return expected, unmatched


def build_rows(root: Path, frame: pd.DataFrame, analysis):
    audit = json.loads((root / 'coverage_audit.json').read_text())
    expected, unmatched = local_pair_identities(root)
    statuses = load_status_details(root)
    old = json.loads(
        (root.parent / 'missing_70_classification.json').read_text()
    )
    old_reasons = {
        (row['id'].rsplit(' [', 1)[0], row['id'].rsplit('[', 1)[-1][:-1]): row[
            'note'
        ]
        for row in old['rows']
    }
    reference_tests, _ = analysis.reference_split(
        frame, analysis.PRE, analysis.POST
    )
    verdicts = {}
    pairs = {(variant, agent) for _, variant, agent in expected}
    for variant, agent in sorted(pairs):
        scored = analysis.score_variant(
            frame, reference_tests, variant, agent=agent
        )
        if scored.empty:
            continue
        for record in analysis.resolved(scored).to_dict('records'):
            verdicts[(record['instance_id'], variant, agent)] = record

    sides = defaultdict(set)
    for record in (
        frame[['instance_id', 'patch_type']]
        .drop_duplicates()
        .to_dict('records')
    ):
        sides[record['instance_id']].add(record['patch_type'])

    rows = []
    for item in sorted(
        (
            row
            for row in audit['rows']
            if (row['instance'], row['variant'], row['model']) in expected
        ),
        key=lambda row: (row['instance'], row['variant']),
    ):
        instance = item['instance']
        variant = item['variant']
        agent = item['model']
        run = latest_run(frame, instance, variant, agent)
        verdict = verdicts.get((instance, variant, agent))
        reference_complete = {'before_patch', 'gold'} <= sides[instance]
        if run.empty:
            tests = passed = failed = None
            detail = statuses.get((instance, variant), {})
            reason = detail.get('reason')
            patch_state = detail.get('patch', 'not checked')
            if not reason:
                note = old_reasons.get((instance, variant))
                reason = failure_reason(note or '')
                if 'failed to apply patch' in (note or '').lower():
                    patch_state = 'failed'
        else:
            tests = len(run)
            passed = int(run['passed'].sum())
            failed = tests - passed
            patch_state = 'applied'
            reason = (
                f'Patch applied; all {tests:,} harness tests passed.'
                if failed == 0
                else (f'Patch applied; {failed:,} of {tests:,} tests failed.')
            )
        if verdict and verdict['f2p'] == 0:
            reason += (
                ' No FAIL_TO_PASS reference tests; resolution is undefined.'
            )
        if not reference_complete:
            missing_details = []
            for side in ('before_patch', 'gold'):
                if side in sides[instance]:
                    continue
                reference = statuses.get((instance, side), {})
                reference_reason = reference.get('reason', 'not run')
                missing_details.append(f'{side}: {reference_reason}')
            reason += (
                ' Reference scoring unavailable ('
                + '; '.join(missing_details)
                + ').'
            )
        rows.append(
            {
                'repository': instance.split('__', 1)[0],
                'instance': instance,
                'set': variant,
                'patch': patch_state,
                'tests': tests,
                'passed': passed,
                'failed': failed,
                'f2p': verdict['f2p'] if verdict else None,
                'f2p_passed': verdict['f2p_passed'] if verdict else None,
                'p2p': verdict['p2p'] if verdict else None,
                'p2p_passed': verdict['p2p_passed'] if verdict else None,
                'p2p_failed': verdict['p2p_failed'] if verdict else None,
                'resolved': (
                    'yes'
                    if verdict and verdict['f2p'] > 0 and verdict['resolved']
                    else 'no'
                    if verdict and verdict['f2p'] > 0
                    else '-'
                ),
                'reason': reason,
            }
        )
    return rows, unmatched


def build_pdf(rows, unmatched, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    width, height = landscape(A4)
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        'Title2',
        parent=styles['Title'],
        fontName='Helvetica-Bold',
        fontSize=22,
        leading=26,
        textColor=colors.HexColor('#13233A'),
        spaceAfter=8,
    )
    subtitle = ParagraphStyle(
        'Subtitle',
        parent=styles['BodyText'],
        fontSize=9,
        leading=13,
        textColor=colors.HexColor('#506176'),
        spaceAfter=10,
    )
    section = ParagraphStyle(
        'Section',
        parent=styles['Heading2'],
        fontSize=12,
        leading=15,
        textColor=colors.HexColor('#13233A'),
        spaceBefore=8,
        spaceAfter=5,
    )
    body = ParagraphStyle(
        'BodySmall',
        parent=styles['BodyText'],
        fontSize=7,
        leading=9,
        textColor=colors.HexColor('#25364D'),
    )
    cell = ParagraphStyle(
        'Cell',
        parent=body,
        fontSize=5.4,
        leading=6.5,
    )
    center = ParagraphStyle('Center', parent=cell, alignment=TA_CENTER)
    reason_style = ParagraphStyle('Reason', parent=cell, alignment=TA_LEFT)
    header_cell = ParagraphStyle(
        'HeaderCell',
        parent=center,
        textColor=colors.white,
        fontName='Helvetica-Bold',
        fontSize=5.4,
        leading=6.5,
    )

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor('#D9E0E8'))
        canvas.line(15 * mm, 10 * mm, width - 15 * mm, 10 * mm)
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(colors.HexColor('#6B7787'))
        canvas.drawString(
            15 * mm, 6 * mm, 'SWE-bench Multimodal evaluation status'
        )
        canvas.drawRightString(width - 15 * mm, 6 * mm, f'Page {doc.page}')
        canvas.restoreState()

    doc = BaseDocTemplate(
        str(destination),
        pagesize=(width, height),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=14 * mm,
        title='SWE-bench Multimodal - Per-instance evaluation status',
    )
    doc.addPageTemplates(
        PageTemplate(
            id='main',
            frames=[
                Frame(
                    doc.leftMargin,
                    doc.bottomMargin,
                    doc.width,
                    doc.height,
                    id='body',
                )
            ],
            onPage=footer,
        )
    )

    completed = sum(row['tests'] is not None for row in rows)
    scored = sum(row['resolved'] != '-' for row in rows)
    resolved_count = sum(row['resolved'] == 'yes' for row in rows)
    sets_by_instance = defaultdict(set)
    for row in rows:
        sets_by_instance[row['instance']].add(row['set'])
    complete_pairs = sum(
        {'with_image', 'without_image'} <= sets
        for sets in sets_by_instance.values()
    )
    story = [
        Paragraph('SWE-bench Multimodal evaluation status', title),
        Paragraph(
            'Per-prediction results for both with_image and without_image. '
            'The patch column records whether the model diff applied. The '
            'reason column separates harness completion, model-test failures, '
            'runner failures, and missing reference baselines.',
            subtitle,
        ),
    ]
    cards = [
        [
            'Prediction rows',
            'Unique instances',
            'Complete pairs',
            'Harness completed',
            'Resolve scored',
            'Resolved yes',
        ],
        [
            str(len(rows)),
            str(len(sets_by_instance)),
            str(complete_pairs),
            str(completed),
            str(scored),
            str(resolved_count),
        ],
    ]
    card_table = Table(
        cards, colWidths=[doc.width / 6] * 6, rowHeights=[9 * mm, 12 * mm]
    )
    card_table.setStyle(
        TableStyle(
            [
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#223C5F')),
                ('BACKGROUND', (0, 1), (-1, 1), colors.HexColor('#F7F9FC')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('TEXTCOLOR', (0, 1), (-1, 1), colors.HexColor('#13233A')),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTNAME', (0, 1), (-1, 1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 7),
                ('FONTSIZE', (0, 1), (-1, 1), 15),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#D9E0E8')),
            ]
        )
    )
    story += [card_table, Spacer(1, 8 * mm)]
    story.append(Paragraph('How to read the table', section))
    story.append(
        Paragraph(
            '<b>Tests / passed / failed</b> describe the model-patch '
            'suite run. <b>Patch</b> reports real git-apply status in the '
            'checked-out repository; “not checked” means execution stopped '
            'before that stage. '
            '<b>F2P, P2P and Resolved</b> require both before_patch and gold '
            'reference results. A dash means scoring is still undefined. '
            '<b>Resolved = no</b> means scoring was possible and the patch '
            'did '
            'not satisfy every required test.',
            body,
        )
    )
    story.append(PageBreak())

    story.append(Paragraph('With-image / without-image pair audit', section))
    story.append(
        Paragraph(
            f'The selected folder contains {len(rows)} prediction rows for '
            f'{len(sets_by_instance)} unique instances: {complete_pairs} '
            'instances have both variants and '
            f'{len(sets_by_instance) - complete_pairs} have only one selected '
            'variant. Missing means no counterpart prediction row exists in '
            'this paired selection; it is not a test failure. Four unmatched '
            'source files were excluded: ' + ', '.join(unmatched) + '.',
            body,
        )
    )
    pair_data = [
        [
            Paragraph('Instance', header_cell),
            Paragraph('with_image', header_cell),
            Paragraph('without_image', header_cell),
            Paragraph('Pair', header_cell),
        ]
    ]
    for instance, sets in sorted(sets_by_instance.items()):
        paired = {'with_image', 'without_image'} <= sets
        pair_data.append(
            [
                Paragraph(instance, cell),
                Paragraph(
                    'present' if 'with_image' in sets else 'missing', center
                ),
                Paragraph(
                    'present' if 'without_image' in sets else 'missing', center
                ),
                Paragraph('complete' if paired else 'incomplete', center),
            ]
        )
    pair_table = Table(
        pair_data,
        colWidths=[90 * mm, 42 * mm, 42 * mm, 35 * mm],
        repeatRows=1,
        hAlign='LEFT',
        splitByRow=1,
    )
    pair_table.setStyle(
        TableStyle(
            [
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#223C5F')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#CDD6E0')),
                (
                    'ROWBACKGROUNDS',
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.HexColor('#F5F7FA')],
                ),
                ('LEFTPADDING', (0, 0), (-1, -1), 3),
                ('RIGHTPADDING', (0, 0), (-1, -1), 3),
                ('TOPPADDING', (0, 0), (-1, -1), 2.5),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5),
            ]
        )
    )
    story.extend([pair_table, PageBreak()])

    grouped = defaultdict(list)
    for row in rows:
        grouped[row['repository']].append(row)
    headers = [
        'Instance',
        'Set',
        'Patch',
        'Tests',
        'Pass',
        'Fail',
        'F2P',
        'F2P pass',
        'P2P',
        'P2P pass',
        'P2P fail',
        'Resolved',
        'Reason',
    ]
    widths = [
        44 * mm,
        21 * mm,
        15 * mm,
        12 * mm,
        12 * mm,
        11 * mm,
        10 * mm,
        13 * mm,
        12 * mm,
        14 * mm,
        14 * mm,
        15 * mm,
        72 * mm,
    ]
    for index, repository in enumerate(sorted(grouped)):
        if index:
            story.append(PageBreak())
        repo_rows = grouped[repository]
        story.append(
            Paragraph(
                f'{repository} - {len(repo_rows)} prediction rows', section
            )
        )
        data = [[Paragraph(header, header_cell) for header in headers]]
        for row in repo_rows:

            def value(name, current_row=row):
                current = current_row[name]
                return 'n/a' if current is None else f'{current:,}'

            data.append(
                [
                    Paragraph(row['instance'], cell),
                    Paragraph(row['set'], cell),
                    Paragraph(row['patch'], center),
                    Paragraph(value('tests'), center),
                    Paragraph(value('passed'), center),
                    Paragraph(value('failed'), center),
                    Paragraph(value('f2p'), center),
                    Paragraph(value('f2p_passed'), center),
                    Paragraph(value('p2p'), center),
                    Paragraph(value('p2p_passed'), center),
                    Paragraph(value('p2p_failed'), center),
                    Paragraph(row['resolved'], center),
                    Paragraph(row['reason'], reason_style),
                ]
            )
        table = Table(
            data, colWidths=widths, repeatRows=1, hAlign='LEFT', splitByRow=1
        )
        table.setStyle(
            TableStyle(
                [
                    (
                        'BACKGROUND',
                        (0, 0),
                        (-1, 0),
                        colors.HexColor('#223C5F'),
                    ),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    (
                        'GRID',
                        (0, 0),
                        (-1, -1),
                        0.25,
                        colors.HexColor('#CDD6E0'),
                    ),
                    (
                        'ROWBACKGROUNDS',
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor('#F5F7FA')],
                    ),
                    ('LEFTPADDING', (0, 0), (-1, -1), 2.2),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 2.2),
                    ('TOPPADDING', (0, 0), (-1, -1), 2.3),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 2.3),
                ]
            )
        )
        story.append(table)
    doc.build(story)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    frame = load_parquets(args.results)
    frame['passed'] = frame['passed'].astype(bool)
    analysis = load_analysis(root)
    rows, unmatched = build_rows(root, frame, analysis)
    build_pdf(rows, unmatched, args.output)
    print(f'Wrote {args.output} with {len(rows)} rows')


if __name__ == '__main__':
    main()
