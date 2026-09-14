# Benchmark repair investigation — 2026-09-09

## Latest verified findings — 2026-09-10

- The report contains 157 complete with/without-image pairs (314 rows).
- `before_patch` incorrectly skipped the maintainer test patch. Corrected
  baseline reruns increased scorable rows from 153 to 203 and resolved rows
  from 47 to 71 without editing result data.
- Historical reference attempts were being merged with repaired attempts.
  Scoring now selects the latest exact reference and model run.
- The shared Karma parser and Highlight.js parser discarded JUnit
  `classname`, causing same-named tests in different suites to collide.
  Test identities are now fully qualified and `<error>` counts as failure.
- OpenLayers launched Chrome with `--disable-gpu`, which caused unrelated
  WebGL failures and stopped suites before benchmark regressions ran. The
  evaluator now uses software WebGL. A four-side canary for OpenLayers 10694
  produced one real F2P test; both model variants ran it and genuinely failed,
  so those rows are now scored `no` rather than left undefined.
- All eight baselines that previously hit the 40-minute canary ceiling
  completed after extending the bounded command and shutdown limits.
- GoogleChrome/Lighthouse remains unscored because its historical checkouts
  need version-aware install, build, and Mocha discovery. These rows remain
  `n/a`; they have not been relabeled.

The live S3 inventory contains 192 distinct model prediction identities
(instance, image variant, model). 128 have matching result objects; 64 do not.
Matching only instance and variant also gives 128 today, but is not a reliable
model-specific coverage rule. The pasted report's 118 instance-and-set pairs
were not an appropriate numerator for this prediction-level denominator.

## Verified correction

50 of the 64 missing predictions fail `git apply --numstat` as uploaded and
pass the same syntax check after appending a final newline. The evaluator now
adds that transport newline when absent. It preserves patch content, hunk
counts, and explicit `No newline at end of file` markers. It does not use
`--reject`, discard failed hunks, or substitute reference code.

Four tests exercise the actual evaluator method using Git in temporary
repositories: missing newline, already valid patch, explicit no-newline
marker, and rejection of mismatched source content. All four pass.

Syntax validity does not establish application success or test completion.
`coverage_audit.json` records each prediction's status and syntax evidence.
It is a baseline snapshot, not a claim that 50 evaluations have completed.

## Worker version mismatch

The first canary stopped before evaluating because the configured AMI's old
`aws/run_ec2.sh` rejected `--apply-test-patch`. That worker was terminated.
`scripts/repair_canary.py` now stages the current Python harness, runner,
dependency manifest, and lockfile on the disposable worker before evaluation.
The temporary staging object is removed afterwards. The worker has a
30-minute shutdown safeguard and is explicitly terminated on exit.

## Additional observed causes

- Carbon 5156: accessibility dependency `@ibma/aat` receives HTML where it
  expects JSON and crashes the test process before JUnit output is flushed.
- Carbon 8912/9136: logs show executable permission failures for `cross-env`.
  The current harness already includes a permission repair that the AMI may
  lack; this needs a new execution to establish recovery.
- OpenLayers 13212: compilation reports a missing `sourcesFromTileGrid`
  export; the browser also reports `regeneratorRuntime` undefined. Zero tests
  execute. This is not merely a misplaced results.xml file.
- Historical logs also include browser timeouts, Docker Hub pull limits,
  unsupported Lighthouse setup, and test-patch path conflicts. Some fixes
  already exist in the current source, making worker version consistency
  necessary before further diagnosis.

Do not label the remaining predictions unfixable based only on the old audit.
Do not count a successful shell command or a valid diff as completed tests.
Canary execution details are recorded in `canary_status.json`.

## Confirmed execution and batch

The replacement canary completed successfully through SSM command
`e4a9394c-0f21-4f5d-8055-476827f52610`. Its result object is
`alibaba%2Dfusion__next%2D1063-without_image-llm.claude4-2026-09-09_19-54-45Z`
in `sbmdt-test-results`: 25,806 bytes containing 1,227 test-result rows.
This establishes at least 129/192 covered prediction identities.

The local poller lost its AWS connection during this run, so its initial
status file recorded a connection error even though the remote command
succeeded. The worker `i-029a04b13ef5098b4` was subsequently confirmed
terminated, and the staging object was deleted. New polling retries
connection failures until the evaluation deadline.

`scripts/run_newline_repairs.py` was started for the other 49 newline-repair
candidates, with three simultaneous workers maximum. It checks current S3
keys to skip already covered predictions. Progress is written to
`repair_batch_status.json` and per-prediction files under `repair-runs/`.
These are execution statuses; final coverage must still be verified against
nonempty uploaded test results. The other 14 missing predictions have valid
patch syntax already and are not included in this targeted batch.
