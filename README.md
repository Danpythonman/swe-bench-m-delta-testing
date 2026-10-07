# swe-bench-m-delta-testing

Delta testing on [SWE-bench Multimodal](https://www.swebench.com/multimodal.html) instances against agent-generated patches, extended to [SWE-bench Verified](https://www.swebench.com/verified.html) (see [SWE-bench Verified](#swe-bench-verified)).

For a given benchmark instance, this project builds a Docker image from the instance's reference Dockerfile, runs the instance's test suite inside a container, and collects per-test pass/fail results. The goal is to compare test outcomes across different patch states (e.g. before a patch, with a patch applied) to see which tests change behavior.

## How it works

1. `sbmdt.evaluate(instance_id)` looks at the instance ID prefix and dispatches to a concrete `Evaluator` (currently only `alibaba-*` instances are supported, via `AlibabaEvaluator`, **this is where help is needed**).

2. The evaluator's `run()` lifecycle (`setup`, then `evaluate`, then `cleanup`) is shared by all evaluators (`src/sbmdt/evaluator/base.py`):
   - **setup**: builds the Docker image from `dockerfiles/<instance_id>/Dockerfile`, starts a container.
   - **evaluate**: runs the test suite inside the container, pulls the resulting JUnit XML out of the container, and parses it into a list of `TestResult` objects (instance, patch type, test name, pass/fail).
   - **cleanup**: stops and removes the container and image.

3. A `Pred` (`src/sbmdt/pred.py`) represents a model-generated patch for an instance and can be loaded from a JSON file with `instance_id`, `model_name_or_path`, and `model_patch` fields.

## Project layout

```
dockerfiles/<instance_id>/Dockerfile   # one Dockerfile per benchmark instance
src/sbmdt/
  interface.py                         # evaluation entrypoint
  benchmark.py                         # SWE-bench M vs Verified, by instance ID prefix
  instance.py                          # Verified instance.json / reference.json
  env.py                               # path constants (project base, dockerfiles dir)
  log.py                               # logging setup
  pred.py                              # Pred: a model-generated patch prediction
  utils.py                             # docker container file read/write/patch helpers
  parquet.py                           # TestResult <-> Parquet, read_test_results
  git_repos.py                         # blobless repository clones under .cache/repos
  aws/ecr.py                           # ECR pull-through cache: base images, login
  analysis/                            # importable analysis (scripts/ are thin CLIs)
    test_split.py                      # classify_tests: F2P/P2P/flaky from runs
    reference.py                       # reference split, model scoring (analyze_results)
    verified.py                        # Verified label matching (compare_official_split)
  evaluator/
    base.py                            # Evaluator ABC, PatchType, TestResult
    alibaba/
      alibaba.py                       # AlibabaEvaluator (Karma-based test runner)
      karma_junit_parser.py            # JUnit XML -> TestResult parsing
    python/                            # shared base for SWE-bench Verified evaluators
      python.py                        # PythonEvaluator, JSON-lines -> TestResult
      pytest_evaluator.py              # PytestEvaluator (10 pytest repositories)
      selection.py                     # touched-scope test selection
      injected/                        # modules copied into containers (Python 3.6+)
    django/django.py                   # DjangoEvaluator (tests/runtests.py)
    sympy/sympy.py                     # SympyEvaluator (bin/test)
scripts/                               # command-line entry points (uv run scripts/...)
notebooks/                             # exploratory analysis
```

Code shared between scripts and notebooks lives in the `sbmdt` package, so
it is imported normally (`from sbmdt.analysis.reference import
reference_split`) instead of by path; a script only parses arguments and
calls into it.

## SWE-bench Verified

The harness also evaluates the 500 SWE-bench Verified instances (12 Python
repositories). Everything except the prebuilt `sweb.eval` Docker images is
this project's own: no SWE-bench harness code is used. Verified's published
FAIL_TO_PASS / PASS_TO_PASS lists are public, which makes them a check on
the delta-testing method: the harness derives its own split from
`before_patch` and `gold` runs and only then compares.

**Instance data** (`dockerfiles/<instance_id>/`, same flat layout as M):

| File | Source |
|---|---|
| `Dockerfile` | `FROM` the prebuilt image via the ECR cache, nothing else |
| `instance.json` | repository, version, base commit (read by the evaluator) |
| `reference.json` | official F2P / P2P lists, **held out**: only `sbmdt.analysis.verified` (`scripts/compare_official_split.py`) reads it |
| `gold_patch.diff` | scraped from the GitHub PR (not the dataset's `patch`) |
| `code_patch.diff`, `test_patch.diff` | path-based split of the gold patch |

**Evaluators** (`src/sbmdt/evaluator/python/`, `django/`, `sympy/`):

- Tests run in *touched* scope: only the test files `test_patch.diff`
  changes, not the whole suite.
- Results are never scraped from console output. A small module of this
  project's own (`evaluator/python/injected/`) is copied into the container
  and hooks the runner, writing one JSON line per outcome:
  a pytest plugin for pytest repositories, a `unittest.TestResult` hook
  around Django's `tests/runtests.py`, and a `PyTestReporter` hook around
  SymPy's `bin/test` (SymPy images ship no pytest). The injected modules
  run under the repository's interpreter, as old as Python 3.6.
- Test names are pytest node IDs, `method (module.Class)` for Django, and
  `path::function` for SymPy.

**Pipeline:**

```bash
uv run scripts/import_swebench.py                  # Dockerfile, instance.json, reference.json
GITHUB_TOKEN=$(gh auth token) uv run scripts/get_gold_patches.py
uv run scripts/split_gold_patch.py
uv run scripts/format_gold_diff_into_pred.py
uv run scripts/audit_gold_apply.py                 # every gold patch applies at its base commit?

# Reference runs (Docker must be logged into the ECR cache, see aws/run_ec2.sh)
uv run scripts/run_instance.py django__django-11099 before_patch --apply-test-patch --json --file
uv run scripts/run_instance.py django__django-11099 gold \
    --pred-file dockerfiles/django__django-11099/gold_patch.pred --json --file

uv run scripts/compare_official_split.py --data results
```

`scripts/analyze_results.py` reads only SWE-bench M rows, so Verified
results can share the S3 bucket without changing M figures.

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) ((**strongly**) recommended) or pip
- Docker, with the daemon running and accessible to the current user

## Installation

```bash
uv sync
```

or, with pip:

```bash
pip install -e .
```

## Usage

```python
from sbmdt import evaluate

evaluate('alibaba-fusion__next-717')
```

See [test.py](test.py) for a runnable example that also wires up logging to `logs/log.log`:

```bash
uv run python test.py
```

## Development

```bash
uv run ruff check .       # lint
uv run ruff format .      # format
uv run pyright            # type check (strict mode)
uv run pre-commit install # set up git hooks
```

Don't forget to make a branch before coding:

```
git checkout -b <name>
```

## Running Containers

First, go to the directory of the container:

```
cd dockerfiles/alibaba-fusion__next-94
```

Then build the image (you give it the name):

```
docker build -t <image-name> .
```

Then run the container (you name the container as well):

```
docker run -it --name <container-name> <image-name> /bin/bash
```

At this point you will be in a Bash shell inside the container. Here you need to figure out how to run the tests and collect results.

If you get errors that container already exists, or if the cleanup code doesn't fully clean up:

```
docker image ls                  # See what images you have
docker image rm <image-name>     # Remove an image
docker container ls -a           # See what containers you have (-a is for stopped containers)
docker container rm <image-name> # Remove a container
```

Then code the pipeline for the repo by:
1. Make a folder `/src/sbmdt/evaluator/<repo>/<repo>.py`.
2. Make sure you make `/src/sbmdt/evaluator/<repo>/__init__.py`.
3. Make a subclass of `Evaluator` class called `<repo>Evaluator`.
4. Code! ;)

Once you code the pipeline for the repo:

```
uv run main.py <instance-id> <patch_type> [--pred-file]
```

Examples:

```
uv run main.py alibaba-fusion__next-717 before_patch

uv run main.py alibaba-fusion__next-717 without_image --pred-file ../Project/image-necessity-paper-analysis/preds/without-images/alibaba-fusion__next-717.pred
```
