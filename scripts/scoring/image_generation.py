"""Keep every instance inside one image generation.

The SWE-bench Multimodal images were rebuilt part-way through this work.
Runs from before and after the rebuild are not interchangeable: for some
instances the new image no longer reproduces the bug at all, so its
before_patch run passes every test. Grading a new-image agent run
against an old-image reference credits the agent for the rebuild, and
grading an old-image agent run against a new reference does the reverse.

So each instance is pinned to a single generation before anything is
scored. The generation chosen is the one that can actually carry a
verdict - a before_patch run that reproduces the bug plus a gold run -
and, among those, the one holding the most agent runs.
"""
import collections
import json
import os

import pandas as pd

REFERENCE_TYPES = ('before_patch', 'gold')

# Runs cluster into campaigns separated by days of silence, and the
# environment moved between them - the images were rebuilt, and the
# later campaigns run a patched harness branch. A gap of two clear
# days is the boundary; the observed gaps are far wider than that.
GAP_DAYS = 2


BOUNDARY_FILE = 'generation_boundaries.json'
_BOUNDS = None


def _boundaries(source='all_test_results.parquet'):
    """Campaign start days, derived once from the whole result set.

    The labels have to mean the same thing to every caller, so they are
    computed from all the data and cached. Deriving them from whatever
    subset a caller happens to hold would give two scripts different
    names for the same campaign.
    """
    global _BOUNDS
    if _BOUNDS is not None:
        return _BOUNDS
    if os.path.exists(BOUNDARY_FILE):
        with open(BOUNDARY_FILE) as fh:
            _BOUNDS = [pd.Timestamp(t) for t in json.load(fh)]
            return _BOUNDS
    days = pd.Index(sorted(set(
        pd.read_parquet(source, columns=['timestamp'])
        .timestamp.dt.normalize())))
    bounds, prev = [], None
    for d in days:
        if prev is None or (d - prev).days > GAP_DAYS:
            bounds.append(d)
        prev = d
    with open(BOUNDARY_FILE, 'w') as fh:
        json.dump([str(t) for t in bounds], fh)
    _BOUNDS = bounds
    return _BOUNDS


def generation_of(timestamps):
    """Label each timestamp with the campaign it belongs to."""
    bounds = pd.DatetimeIndex(_boundaries()).tz_convert('UTC')
    day = pd.to_datetime(timestamps).dt.tz_convert('UTC').dt.normalize()
    idx = bounds.searchsorted(pd.DatetimeIndex(day), side='right') - 1
    return pd.Series(['gen%d' % i for i in idx], index=day.index)


def pin_to_one_generation(frame, verbose=True):
    """Drop runs that do not belong to each instance's chosen generation."""
    # Off by default. Discarding a whole campaign was the first
    # answer to cross-campaign contamination; grading each run
    # against its own campaign's reference is the better one, and it
    # keeps the runs this would throw away. Kept behind a flag
    # because it is still the sharpest way to ask what the numbers
    # look like on one environment alone.
    if not os.environ.get('SBMDT_PIN'):
        return frame, {}
    gen = generation_of(frame.timestamp)
    runs = (frame.assign(gen=gen)
            .groupby(['instance_id', 'gen', 'patch_type', 'agent_name',
                      'timestamp'])
            .passed.agg(fail=lambda s: int((~s).sum())).reset_index())

    choice, mixed, single = {}, 0, 0
    for inst, sub in runs.groupby('instance_id'):
        best, best_key = None, None
        for g, gs in sub.groupby('gen'):
            repro = not gs[(gs.patch_type == 'before_patch')
                           & (gs.fail > 0)].empty
            gold = not gs[gs.patch_type == 'gold'].empty
            agents = len(gs[~gs.patch_type.isin(REFERENCE_TYPES)])
            key = (1 if (repro and gold) else 0, agents)
            if best_key is None or key > best_key:
                best, best_key = g, key
        if sub.gen.nunique() > 1:
            mixed += 1
        else:
            single += 1
        # Nothing usable anywhere: keep both rather than delete the
        # instance, and let the scorer report it as ungradeable.
        choice[inst] = best if best_key[0] else None

    keep = pd.Series(
        [choice.get(i) in (None, g) for i, g in zip(frame.instance_id, gen, strict=False)],
        index=frame.index)
    if verbose:
        pinned = sum(1 for v in choice.values() if v)
        # This used to name a single cutoff date, `CUT`, and count the
        # two labels 'old' and 'new'. Campaigns are now derived from
        # `_boundaries()` and labelled gen0..genN, so CUT no longer
        # exists - the message raised NameError on the one path that
        # printed it - and the two counts were always zero.
        print('image generations: %d instance(s) have runs in more than '
              'one campaign' % mixed)
        spread = collections.Counter(v for v in choice.values() if v)
        print('  pinned to one campaign: %d  (%s)'
              % (pinned,
                 ', '.join('%s %d' % kv for kv in sorted(spread.items()))
                 or 'none'))
        print('  no generation carries a reference: %d'
              % sum(1 for v in choice.values() if v is None))
        print('  rows dropped as cross-generation: %d of %d'
              % (int((~keep).sum()), len(frame)))
    return frame[keep], choice
