# Scoring and reporting

These build the FAIL_TO_PASS / PASS_TO_PASS reference from the harness's
own before_patch and gold runs, grade the agent runs against it, and
write the per-instance deliverables.

They are not a library. They run from a **data directory** holding the
results synced out of S3, which is not this repository — set
`SBMDT_REPO` to point back here if that directory is somewhere else:

```bash
export SBMDT_REPO=/path/to/swe-bench-m-delta-testing
```

Run them in this order; each reads what the previous one wrote.

| Script | Reads | Writes |
| --- | --- | --- |
| `image_generation.py` | — | imported by the others; labels each run with its campaign |
| `flaky_tests.py` | `all_test_results.parquet`, `identical_arms.json` | `flaky_tests.csv` - tests that flip on byte-identical code |
| `score_final.py` | `all_test_results.parquet`, `$SBMDT_REPO/dockerfiles/*/test_patch.diff`, `flaky_tests.csv` | `reference_final.json`, `reference_by_generation.json` |
| `score_agents.py` | the reference | `scored_final.csv` |
| `agent_instance_detail.py` | the reference, `scored_final.csv` | `agent_instance_detail.csv` |
| `mk_agent_xlsx.py` | `agent_instance_detail.csv` | `agent_instances_all.xlsx`, one workbook per agent |
| `mk_result_csv.py` | `agent_instance_detail.csv` | `result.csv` |

## Why the reference is rebuilt rather than read

The official FAIL_TO_PASS lists are redacted in every copy of the
benchmark available here, so the reference is derived from the runs
themselves using the project's own classifier
(`notebooks/test_split.py`) rather than a second, privately
reimplemented one. The deliverables and the harness then agree by
construction.

## The one rule that matters most

A reference's `before_patch` and `gold` runs **must come from the same
campaign**. `choose_run` used to pick the newest usable run of each kind
independently, so a pair could straddle the September image rebuild.
When it did, every test the newer side ran and the older side never did
was absent from the base and passing in gold, which the classifier reads
as FAIL_TO_PASS.

Measured on 2026-09-22, seven openlayers instances carried 115-163
FAIL_TO_PASS entries built this way, of which only 29-40 had ever been
observed failing. Every arm passed all of them, which is what made it
visible: agents whose patches contain no source change do not sweep 158
tests. The `MIN_PRE_RUN_COVERAGE = 0.95` guard in the classifier does
not catch this, because it is a ratio and the harm is absolute — 5% of
a 2,495-test suite is 124 phantom entries, against a median real test
patch of 2 tests.

`choose_run` now requires the pair to share a campaign and drops the
instance when no same-campaign pair reproduces the bug. That costs
coverage: 429 gradeable instances become 324. It is the right trade,
because the alternative is a reference that scores runs as resolved for
having been made after an image rebuild.

Hardening the classifier guard itself is still open. An absolute cap has
little margin — legitimate test patches reach 67 tests and the
contaminated shortfalls start at 75 — and the clean alternative,
refusing pre-absent tests for instances the test patch does not anchor,
would zero out FAIL_TO_PASS for instances whose patch adds tests whose
titles do not parse.

## Flaky tests

GUIRepair's two conditions carry byte-identical patches, and so do 152 of
OpenHands'. Run in the same campaign on the same harness, each such pair is
one piece of code measured twice, so a test that passes in one run and fails
in the other is flaky by observation. `flaky_tests.py` lists every test that
does this on at least two instances (one instance could be a single agent's
racy patch), and `score_final.py` drops them from every reference for every
agent alike.

Measured on 23 September: 49 tests, 46 of them openlayers, led by the
`ol/View #animate()` and `#cancelAnimations()` timing tests at 18-26
instances each. 23 openlayers instances had a FAIL_TO_PASS list made only
of such tests - their verdicts were whichever way a timing test fell - and
are no longer gradeable.

## Three more ways a verdict could rest on something other than the patch

Found by a second pass on 23 September; each has a case in
`scripts/test_reference_rules.py`.

- **A title repeated within one run.** carbon reports its Public API
  snapshot test twice per run and prettier reports `snippet: #0 format`
  five times. Pairing a before_patch run with a gold run kept whichever
  copy came last, so a failing copy could vanish and the pair looked like
  it reproduced nothing. `image_generation.run_verdicts` fails a test if
  any copy fails, the rule `classify_tests` already applied. 21 instances
  (17 carbon, 4 prettier) regained a reference.
- **Named tests that already pass.** FAIL_TO_PASS is anchored to the
  tests the test patch names. When every one of those passes before the
  fix, the anchored list is satisfied by an empty patch; the classifier
  now falls back to the tests that do fail before the fix (carbon-12420:
  the Public API snapshot, not the new TimePicker test).
- **Tests the harness never runs.** The openlayers evaluator runs karma
  only. An instance whose test patch touches nothing outside
  `test/rendering/` gets no reference: whatever flips in the unit suite is
  unrelated to the fix (openlayers-13013 and -15685 were graded on a View
  animation test and a font-loading test).

And one on the agent side: an agent run that reports under 90% of its
reference's tests is not graded - the same bar `score_final.py` applies to
reference runs. A complete run from the same campaign is used instead;
with none, the cell is reported as stopped part-way, not unresolved.
alibaba-4182 stops at test 117 of ~1,550 patched and unpatched alike.
