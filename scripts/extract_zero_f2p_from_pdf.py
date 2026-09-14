"""Extract the current report instances whose displayed F2P count is zero."""

import json
import re
from pathlib import Path

from pypdf import PdfReader


def main() -> None:
    pdf = Path('output/pdf/swe_bench_multimodal_instance_status.pdf')
    lines = [
        line.strip()
        for page in PdfReader(pdf).pages
        for line in (page.extract_text() or '').splitlines()
    ]
    instances = set()
    for index, line in enumerate(lines):
        if not re.match(r'^[A-Za-z0-9.-]+__.+-\d+$', line):
            continue
        if index + 6 < len(lines) and lines[index + 6] == '0':
            instances.add(line)
    print(f'{len(instances)} instances')
    print('\n'.join(sorted(instances)))
    affected = sorted(
        instance
        for instance in instances
        if (Path('dockerfiles') / instance / 'test_patch.diff').is_file()
        and (Path('dockerfiles') / instance / 'test_patch.diff').stat().st_size
    )
    Path('zero_f2p_candidates.json').write_text(
        json.dumps({'instances': affected}, indent=2), encoding='utf-8'
    )


if __name__ == '__main__':
    main()
