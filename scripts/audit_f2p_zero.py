"""List instances whose current dynamic reference split has no F2P test."""

from pathlib import Path

import build_benchmark_pdf as report


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    frame = report.load_parquets(root / 'live-results')
    frame['passed'] = frame['passed'].astype(bool)
    analysis = report.load_analysis(root)
    expected, _ = report.local_pair_identities(root)
    report_instances = {instance for instance, _, _ in expected}
    _, split = analysis.reference_split(frame, analysis.PRE, analysis.POST)
    complete = analysis.both_sides(frame, analysis.PRE, analysis.POST)
    instances = sorted(
        instance
        for instance in complete & report_instances
        if not split.fail_to_pass.get(instance, [])
    )
    print(f'{len(instances)} instances', flush=True)
    print('\n'.join(instances), flush=True)


if __name__ == '__main__':
    main()
