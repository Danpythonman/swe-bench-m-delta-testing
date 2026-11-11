# SWE-DT derived labels

FAIL_TO_PASS and PASS_TO_PASS tests for SWE-bench Verified and SWE-bench
Multimodal, derived by delta testing: each instance's tests are run with only
the pull request's test changes applied (`before_patch`) and again with the
whole pull request applied (`gold`), and every test is classified by how its
outcome changes. The official benchmark labels are never read while deriving
these. Built by `scripts/export_dataset.py` from the reference runs in
`s3-sync/test-results`; nothing is rerun.

| Benchmark | Instances | FAIL_TO_PASS | PASS_TO_PASS | Unlabelled tests |
|---|---:|---:|---:|---:|
| SWE-bench Verified | 498 of 500 | 931 | 65,770 | 885 broken |
| SWE-bench Multimodal | 489 of 510 | 3,004 | 1,458,366 | 2,289 broken, 563 regressed, 486 not executed |

## Files

| File | Contents |
|---|---|
| `verified_labels.jsonl.gz`, `multimodal_labels.jsonl.gz` | One JSON object per instance: `instance_id`, `FAIL_TO_PASS`, `PASS_TO_PASS` (sorted lists of test names). The same field names as SWE-bench, so the lists can replace the official ones. |
| `verified_tests.parquet`, `multimodal_tests.parquet` | One row per classified test (columns below), including the tests that get no label. |
| `SHA256SUMS` | Checksums of the four files. The export is deterministic, so rerunning the script reproduces them. |

Columns of `*_tests.parquet`:

| Column | Meaning |
|---|---|
| `instance_id` | Benchmark instance, e.g. `django__django-11099`. |
| `test_name` | The test as the runner reports it: a pytest node id (`path::Class::test[param]`), a Django id (`method (module.Class)`), a SymPy function name, or a JavaScript suite and test title. In Multimodal, a name that more than one file defines in an instance is written `file::name`. |
| `test_file` | File the test is defined in, where the runner reports it (most JavaScript runners); empty otherwise. |
| `category` | `FAIL_TO_PASS`, `PASS_TO_PASS`, or a category that gets no label: `BROKEN` (fails both before and after), `REGRESSED` (passes before, fails after), `FLAKY` (outcome varies across identical runs), `NOT_EXECUTED` (see below). |
| `pre_state` | The test's state in the `before_patch` run: `PASS`, `FAIL`, `ERROR` or `NOT_EXECUTED`. |
| `added` | Whether the PR's test changes add the test. |

## Test states

A test the `before_patch` run reports is `PASS` or `FAIL`. A test it does not
report, although the `gold` run does, is

* `ERROR` when an error in the run explains its absence: a pytest collection
  error for its file, a unittest `setUpClass`/`setUpModule` failure or
  `_FailedTest`, a Jest "encountered a declaration exception" in its file, a
  failed Mocha `before all`/`before each` hook, or the PR's test changes adding
  the test. `ERROR` counts as failing, so such a test can be FAIL_TO_PASS
  (56 in Verified, 765 in Multimodal);
* `NOT_EXECUTED` when nothing explains it, for example because the runner
  stopped early. These tests get no label (486 tests in 35 Multimodal
  instances).

## Which runs

* **Verified**: the latest `before_patch` and `gold` run of each instance.
  `django__django-14672` and `pylint-dev__pylint-4661` have no pair of
  reference runs and are not included.
* **Multimodal**: only reference runs from harness commit `86a48a4`, the
  rerun of 2026-11-03 after the September evaluator fixes. An instance whose
  rerun failed on either side is left out rather than mixed with runs from
  older code. 21 instances are not included: 14 `quarto-dev/quarto-cli`,
  `alibaba-fusion/next` 870, 4806 and 4859, `lighthouse` 1446 and 1549 and
  `openlayers` 15466 (the PR's tests import code only the fix adds, so the
  before run cannot start), and `prettier` 16347 (timeout).

## Known caveats

* Labels reflect the reference fix: an incomplete fix or an
  implementation-specific test carries over into the labels.
* `carbon-design-system__carbon-12410`: the gold run checks out the PR head,
  a merge of `main`, which brings in unrelated tests.
* `prettier__prettier-14688`: the reference runs are unstable (about half the
  suite fails on both sides).
* Large FAIL_TO_PASS lists (over 20 tests) in 12 Multimodal instances, e.g.
  `prettier__prettier-9514` (733) and `prettier__prettier-14688` (578), most
  likely come from renamed or regenerated suites rather than new tests.
* Network-dependent tests (e.g. in `psf/requests`) and unpinned build-time
  downloads in the Multimodal images can change outcomes over time.

## Reproducing

```
aws/sync-s3-with-local.sh          # or any copy of the result objects
uv run scripts/export_dataset.py   # writes dataset/, checks it, prints counts
(cd dataset && sha256sum -c SHA256SUMS)
```

## License

The derived labels and per-test states are released under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The test names
come from the benchmark repositories, under their own licenses; the benchmark
instances are from SWE-bench (MIT). The code is MIT (see `../LICENSE`).
