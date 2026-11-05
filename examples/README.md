# Example agent patches

Three hand-written patches for `django__django-11099`, so the whole
workflow can be tried without an agent of your own. They are illustrations
written for this repository, not output from a real agent.

The instance: Django's username validators use the regex `^[\w.@+-]+$`,
and `$` also matches before a trailing newline, so `"user\n"` is accepted.
The maintainers' fix changes `$` to `\Z`.

| File | What the patch does | Expected verdict |
| --- | --- | --- |
| `django__django-11099.correct.pred` | Fixes the bug differently from the maintainers (`\A...\Z`) | RESOLVED: both F2P tests pass, no P2P test lost |
| `django__django-11099.noop.pred` | Only adds a comment | UNRESOLVED: neither F2P test passes |
| `django__django-11099.regression.pred` | Fixes the bug, but also rewords the numeric-password error message | UNRESOLVED: both F2P tests pass, but the P2P test `NumericPasswordValidatorTest.test_validate` fails |

A `.pred` file is one JSON object: `instance_id`, `model_name_or_path`
(used as the agent's name) and `model_patch` (a unified diff against the
instance's base commit).

## Running them

From the repository root, with Docker running (a few minutes in total; the
image is pulled on the first run):

```bash
I=django__django-11099

# Reference runs: before the fix (test half of the PR only) and with it
uv run scripts/run_instance.py $I before_patch --apply-test-patch --json --file
uv run scripts/run_instance.py $I gold \
    --pred-file dockerfiles/$I/gold_patch.pred --json --file

# The three example patches
for p in correct noop regression; do
    uv run scripts/run_instance.py $I with_image \
        --pred-file examples/$I.$p.pred --apply-test-patch --json --file
done

# Derive the split and grade every patch
uv run scripts/derive_and_grade.py --instance $I --list
```

Expected output (the derived split, then one verdict per patch, with the
tests each patch did not pass):

```
Derived split (before_patch vs gold):
  django__django-11099  2 F2P, 20 P2P
      F2P  test_ascii_validator (auth_tests.test_validators.UsernameValidatorsTests)
      F2P  test_unicode_validator (auth_tests.test_validators.UsernameValidatorsTests)

Agent patches:
  django__django-11099  example-correct (with_image): RESOLVED    F2P 2/2 passed, P2P 0 of 20 lost
  django__django-11099  example-noop (with_image): UNRESOLVED  F2P 0/2 passed, P2P 0 of 20 lost
      F2P failed   test_ascii_validator (auth_tests.test_validators.UsernameValidatorsTests)
      F2P failed   test_unicode_validator (auth_tests.test_validators.UsernameValidatorsTests)
  django__django-11099  example-regression (with_image): UNRESOLVED  F2P 2/2 passed, P2P 1 of 20 lost
      P2P failed   test_validate (auth_tests.test_validators.NumericPasswordValidatorTest)
```

SWE-bench Verified's own labels for this instance list a third F2P test,
`UserAttributeSimilarityValidatorTest.test_help_text`. It already passes
before the fix, so the derived split counts it as P2P instead.
