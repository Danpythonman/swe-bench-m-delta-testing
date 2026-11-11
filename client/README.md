# swe-dt

Client for the hosted [SWE-DT](https://github.com/Danpythonman/swe-bench-m-delta-testing)
evaluation API. It grades coding-agent patches for SWE-bench Verified and
SWE-bench Multimodal against SWE-DT's delta-tested FAIL_TO_PASS and
PASS_TO_PASS labels. Each patch runs on a fresh cloud worker, so you don't
need Docker or the benchmark images.

```bash
pip install swe-dt
export SWE_DT_API_KEY=...   # ask the maintainers for a key
```

## Command line

```bash
swe-dt instances                                   # what can be evaluated
swe-dt submit fix.diff --instance django__django-11099 --wait
swe-dt submit preds.jsonl --wait                   # SWE-bench predictions
swe-dt status <id>
```

`submit` takes either a plain unified diff with `--instance`, or SWE-bench
predictions: a `.pred`/`.json` file holding one object or a list of them, or
a `.jsonl` file with one object per line. Each prediction needs
`instance_id` and `model_patch`. `model_name_or_path` becomes the agent
name. With `--wait`, the command waits whenever all workers are busy, and
prints one verdict per patch:

```
django__django-11099  example-regression  DONE  UNRESOLVED  F2P 2/2 passed, P2P 1 of 20 lost
    P2P lost    test_validate (auth_tests.test_validators.NumericPasswordValidatorTest)
```

## Python

```python
from swe_dt import Client

client = Client()                       # or Client(api_key=...)
evaluation = client.evaluate('django__django-11099', patch, agent_name='my-agent')
evaluation['result']
# {'resolved': False, 'f2p': 2, 'f2p_passed': 2, 'p2p': 20, 'p2p_lost': 1,
#  'failed_f2p': [], 'lost_p2p': ['test_validate (...)'], ...}
```

`submit()` and `status()` / `wait()` split `evaluate()` into its steps.

A patch resolves an instance when every FAIL_TO_PASS test passes and no
PASS_TO_PASS test is lost. A test that does not run counts against the patch.

## Limits

- 20 evaluations per key per day.
- 5 evaluations at once across all users.
- Patches up to 1 MB.
- 60 minutes per evaluation.

The labels themselves are released under CC BY 4.0 in the repository's
[`dataset/`](https://github.com/Danpythonman/swe-bench-m-delta-testing/tree/master/dataset)
directory.
