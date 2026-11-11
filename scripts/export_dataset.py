"""Export the derived FAIL_TO_PASS / PASS_TO_PASS dataset to ``dataset/``.

Reads the reference runs already synced to ``s3-sync/test-results`` (no
test is run) and classifies them with
:func:`sbmdt.analysis.reference.reference_split`, the same code and the
same run selection as the paper's agreement table:

* **SWE-bench Verified**: the latest ``before_patch`` and ``gold`` run of
  each instance.
* **SWE-bench Multimodal**: only the reference runs made from
  ``REFERENCE_COMMIT`` (the 2026-11-03 rerun after Parsa's September
  evaluator fixes). An instance whose rerun failed on either side has no
  reference and is left out instead of falling back to older code.

For each benchmark it writes

* ``<benchmark>_labels.jsonl.gz``: one JSON object per instance with
  ``instance_id``, ``FAIL_TO_PASS`` and ``PASS_TO_PASS`` (sorted test
  names), the field names SWE-bench uses;
* ``<benchmark>_tests.parquet``: one row per classified test with its
  category (including the tests that get no label: REGRESSED, BROKEN,
  FLAKY, NOT_EXECUTED), its before-patch state (PASS, FAIL, ERROR,
  NOT_EXECUTED) and whether the test patch added it;

then reads both files back and checks that they hold exactly the split it
derived, and writes ``SHA256SUMS``.

Usage:
    uv run scripts/export_dataset.py
    uv run scripts/export_dataset.py --out /tmp/dataset
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import tempfile
from pathlib import Path
from typing import Final
from urllib.parse import unquote

import pandas as pd

from sbmdt.analysis.reference import (
    FILE,
    INSTANCE,
    POST,
    PRE,
    TEST,
    load_results,
    reference_split,
)
from sbmdt.analysis.verified import load_verified_results
from sbmdt.benchmark import Benchmark, benchmark_of
from sbmdt.env import PROJECT_BASE

RESULTS: Final[Path] = PROJECT_BASE / 's3-sync' / 'test-results'
# Every Multimodal reference was rerun on 2026-11-03 from this commit; see
# notebooks/m_v2_discrepancy.ipynb, whose run selection this repeats.
REFERENCE_COMMIT: Final[str] = '86a48a454c43b7e58f17443f1bb446b8ab8ec409'
# A result object is named <instance>-<patch type>-<agent>-<time>.
INSTANCE_PREFIX: Final[re.Pattern[str]] = re.compile(r'^(.+?__.+?-\d+)-')
LABELS: Final[tuple[str, str]] = ('FAIL_TO_PASS', 'PASS_TO_PASS')
COLUMNS: Final[list[str]] = [
    INSTANCE,
    TEST,
    FILE,
    'category',
    'pre_state',
    'added',
]


def verified_split(results: Path) -> pd.DataFrame:
    """Classify every Verified instance with both reference runs.

    ``load_verified_results`` reads a whole folder, so it gets a temporary
    one linking only the Verified result objects, as the agreement analysis
    does (the bucket holds about 11,000 objects, most of them Multimodal).
    """
    with tempfile.TemporaryDirectory() as tmp:
        for path in results.iterdir():
            match = INSTANCE_PREFIX.match(unquote(path.name))
            if (
                match
                and benchmark_of(match[1]) == Benchmark.SWE_BENCH_VERIFIED
            ):
                (Path(tmp) / f'{path.name}.parquet').symlink_to(path.resolve())
        frame = load_verified_results(Path(tmp))
    tests, _ = reference_split(frame, PRE, POST)
    return with_files(tests, frame)


def multimodal_split(results: Path) -> pd.DataFrame:
    """Classify every Multimodal instance with both current references."""
    frame = load_results(results)
    is_reference = frame['patch_type'].isin([PRE, POST])
    frame = frame[
        ~is_reference | (frame['harness_commit'] == REFERENCE_COMMIT)
    ]
    tests, _ = reference_split(frame, PRE, POST)
    return with_files(tests, frame)


def with_files(tests: pd.DataFrame, frame: pd.DataFrame) -> pd.DataFrame:
    """Add the file each test is defined in, where a reference run says.

    The split carries only names; the runs record the file for the
    evaluators that report it. Python runners that do not (Django, SymPy)
    leave it empty; a pytest name carries its file before ``::``.
    """
    if FILE not in frame.columns:
        return tests.assign(**{FILE: None})
    files = frame[
        frame['patch_type'].isin([PRE, POST]) & frame[FILE].notna()
    ].drop_duplicates([INSTANCE, TEST])[[INSTANCE, TEST, FILE]]
    return tests.drop(columns=[FILE], errors='ignore').merge(
        files, on=[INSTANCE, TEST], how='left', validate='many_to_one'
    )


def labels(tests: pd.DataFrame) -> list[dict[str, object]]:
    """One SWE-bench-style record per instance."""
    records = []
    for instance, group in tests.groupby(INSTANCE, sort=True):
        record: dict[str, object] = {'instance_id': str(instance)}
        for label in LABELS:
            names = group.loc[group['category'] == label, TEST]
            record[label] = sorted(map(str, names))
        records.append(record)
    return records


def write(name: str, tests: pd.DataFrame, out: Path) -> dict[str, int]:
    """Write one benchmark's files, read them back, and check them."""
    tests = tests.copy()
    if FILE not in tests.columns:
        tests[FILE] = None
    tests = tests[COLUMNS].sort_values([INSTANCE, TEST], ignore_index=True)
    for column in (INSTANCE, TEST, FILE, 'category', 'pre_state'):
        tests[column] = tests[column].astype('string')
    tests['added'] = tests['added'].astype(bool)

    records = labels(tests)
    label_path = out / f'{name}_labels.jsonl.gz'
    # mtime=0 keeps the archive byte-identical across exports.
    with (
        gzip.GzipFile(label_path, 'wb', mtime=0) as raw,
        io.TextIOWrapper(raw, encoding='utf-8') as handle,
    ):
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    table_path = out / f'{name}_tests.parquet'
    tests.to_parquet(table_path, index=False)

    _check(tests, label_path, table_path)
    counts = {
        'instances': len(records),
        **{
            label: int((tests['category'] == label).sum())
            for label in tests['category'].dropna().unique()
        },
    }
    return counts


def _check(tests: pd.DataFrame, label_path: Path, table_path: Path) -> None:
    """Fail unless both files hold exactly the derived split."""
    expected = {
        (str(i), label): set(map(str, g[TEST]))
        for (i, label), g in tests[tests['category'].isin(LABELS)].groupby(
            [INSTANCE, 'category']
        )
    }
    found: dict[tuple[str, str], set[str]] = {}
    with gzip.open(label_path, 'rt', encoding='utf-8') as handle:
        for line in handle:
            record = json.loads(line)
            for label in LABELS:
                if record[label]:
                    found[(record['instance_id'], label)] = set(record[label])
                    if len(record[label]) != len(set(record[label])):
                        raise SystemExit(
                            f'{label_path}: duplicate names in '
                            f'{record["instance_id"]} {label}'
                        )
    if found != expected:
        raise SystemExit(f'{label_path} does not match the derived split')
    back = pd.read_parquet(table_path)
    if len(back) != len(tests) or back[TEST].isna().any():
        raise SystemExit(f'{table_path} does not match the derived split')
    if back.duplicated([INSTANCE, TEST]).any():
        raise SystemExit(f'{table_path}: a test appears twice')


def checksums(out: Path) -> None:
    lines = []
    for path in sorted(out.glob('*_*.*')):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f'{digest}  {path.name}')
    (out / 'SHA256SUMS').write_text('\n'.join(lines) + '\n')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument(
        '--results',
        type=Path,
        default=RESULTS,
        help='Synced test-results directory (default: s3-sync/test-results).',
    )
    parser.add_argument(
        '--out',
        type=Path,
        default=PROJECT_BASE / 'dataset',
        help='Output directory (default: dataset/).',
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    for name, split in (
        ('verified', verified_split),
        ('multimodal', multimodal_split),
    ):
        counts = write(name, split(args.results), args.out)
        print(name, json.dumps(counts))
    checksums(args.out)


if __name__ == '__main__':
    main()
