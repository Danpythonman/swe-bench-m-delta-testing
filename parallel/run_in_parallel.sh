#!/usr/bin/env bash
#
# Runs the commands/tasks listed in tasks.txt using GNU parallel,
# writing per-job output and a joblog to ../outdir (created if missing).
#
# Usage:
#   ./run-parallel.sh
#
# Requires: GNU parallel (https://www.gnu.org/software/parallel/)

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
TASKS_FILE="${SCRIPT_DIR}/tasks.txt"
OUTDIR="${SCRIPT_DIR}/../outdir"
JOBLOG="${OUTDIR}/log.txt"

if [[ ! -f "${TASKS_FILE}" ]]; then
    echo "Error: task list not found: ${TASKS_FILE}" >&2
    exit 1
fi

mkdir -p "${OUTDIR}"

parallel \
    -a "${TASKS_FILE}" \
    --results "${OUTDIR}/job{#}.out" \
    --joblog "${JOBLOG}" \
    --resume
