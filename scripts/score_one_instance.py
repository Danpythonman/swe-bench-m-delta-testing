"""Quickly score one instance from its latest exact runs."""

import argparse

from build_benchmark_pdf import load_parquets


def latest(frame, instance, patch):
    part = frame[
        (frame['instance_id'] == instance) & (frame['patch_type'] == patch)
    ]
    if part.empty:
        return part
    return part[part['timestamp'] == part['timestamp'].max()]


def outcomes(part):
    return dict(
        zip(part['test_name'], part['passed'].astype(bool), strict=True)
    )


def main(instance: str, variant: str) -> None:
    frame = load_parquets(__import__('pathlib').Path('live-results'))
    before = outcomes(latest(frame, instance, 'before_patch'))
    gold = outcomes(latest(frame, instance, 'gold'))
    model = outcomes(latest(frame, instance, variant))
    for patch in ('before_patch', 'gold', variant):
        part = latest(frame, instance, patch)
        print(
            patch,
            len(part),
            list(part['timestamp'].astype(str).unique()),
            list(part['test_name'].head(2)),
        )
    common = before.keys() & gold.keys()
    f2p = sorted(name for name in common if not before[name] and gold[name])
    p2p = sorted(name for name in common if before[name] and gold[name])
    print(
        f'F2P={len(f2p)} passed={sum(model.get(name) is True for name in f2p)}'
    )
    print(
        f'P2P={len(p2p)} passed={sum(model.get(name) is True for name in p2p)}'
    )
    print(f'not_run={sum(name not in model for name in f2p + p2p)}')
    print(
        'resolved='
        + str(bool(f2p) and all(model.get(name) is True for name in f2p + p2p))
    )
    print('\n'.join(f2p))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('instance')
    parser.add_argument('variant')
    args = parser.parse_args()
    main(args.instance, args.variant)
