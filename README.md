# swe-bench-m-delta-testing

Delta testing on [SWE-bench Multimodal](https://www.swebench.com/multimodal.html) instances against agent-generated patches.

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
  env.py                               # path constants (project base, dockerfiles dir)
  log.py                               # logging setup
  pred.py                              # Pred: a model-generated patch prediction
  utils.py                             # docker container file read/write/patch helpers
  evaluator/
    base.py                            # Evaluator ABC, PatchType, TestResult
    alibaba/
      alibaba.py                       # AlibabaEvaluator (Karma-based test runner)
      karma_junit_parser.py            # JUnit XML -> TestResult parsing
```

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
