#!/bin/bash
# One hosted-API evaluation. aws/api/handler.py fills the __NAMES__ in and
# passes this as the worker's user data; cloud-init runs it once, as root.
#
# The worker has no IAM role. It fetches its patch from the API and reports
# back to it with the job's token, then powers off; the launch template sets
# "terminate on shutdown", so powering off ends the instance.

# Hard stop first, so the instance ends even if everything below hangs.
shutdown -h +__RUN_MINUTES__

exec >/var/log/sbmdt-job.log 2>&1
set -u

export HOME=/root
export PATH=/usr/local/bin:/usr/bin:/bin
export UV_PYTHON_INSTALL_DIR=/opt/uv/python
export SBMDT_REGISTRY=docker.io

API='__API__'
JOB='__JOB__'
TOKEN='__TOKEN__'
INSTANCE='__INSTANCE__'
COMMIT='__COMMIT__'

# report STATUS [ERROR]: POST the verdict (if any), the error and the end of
# the run log to the API.
report() {
    python3 - "$1" "${2:-}" >/tmp/report.json <<'PY'
import json, pathlib, sys
status, error = sys.argv[1], sys.argv[2]
out = {'status': status}
if error:
    out['error'] = error
verdict = pathlib.Path('/tmp/verdict.json')
if status == 'DONE' and verdict.exists():
    out['verdict'] = json.loads(verdict.read_text())
log = pathlib.Path('/tmp/run.log')
if log.exists():
    out['log'] = '\n'.join(log.read_text(errors='replace').splitlines()[-400:])
print(json.dumps(out))
PY
    for attempt in 1 2 3; do
        curl -fsS -X POST -H "X-Job-Token: ${TOKEN}" \
            -H 'Content-Type: application/json' \
            --data-binary @/tmp/report.json \
            "${API}/v1/jobs/${JOB}/result" && return 0
        sleep $((attempt * 10))
    done
}

fail() {
    echo "worker: $1"
    report FAILED "$1"
    shutdown -h now
    exit 0
}

cd /opt/sbmdt || fail 'no checkout on the worker image'
git -c safe.directory=/opt/sbmdt fetch --depth 1 origin "${COMMIT}" \
    || fail "could not fetch harness commit ${COMMIT}"
git -c safe.directory=/opt/sbmdt checkout -f "${COMMIT}" \
    || fail "could not check out ${COMMIT}"
uv sync --frozen || fail 'could not install the harness'

curl -fsS -H "X-Job-Token: ${TOKEN}" "${API}/v1/jobs/${JOB}/patch" \
    -o /tmp/job.pred || fail 'could not fetch the patch'

timeout __TEST_MINUTES__m uv run scripts/run_instance.py "${INSTANCE}" \
    with_image --apply-test-patch --pred-file /tmp/job.pred \
    --json --file --log-file /tmp/run.log
code=$?
if [[ ${code} -eq 124 ]]; then
    fail 'the tests did not finish within __TEST_MINUTES__ minutes'
fi

uv run scripts/grade_against_dataset.py "${INSTANCE}" --out /tmp/verdict.json \
    || fail "the run (exit ${code}) produced no results to grade; see the log"

report DONE
shutdown -h now
