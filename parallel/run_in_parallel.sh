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
OUTDIR="${SCRIPT_DIR}/outdir"
JOBLOG="${OUTDIR}/joblog.txt"
CLEANUP_SCRIPT="${SCRIPT_DIR}/docker_cleanup.sh"
DOCKER_CLEANUP_INTERVAL='30'

# Parse args, enabling Docker cleanup if requested
DOCKER_CLEANUP=0
for arg in "$@"; do
    case "${arg}" in
        --docker-cleanup)
            DOCKER_CLEANUP=1
            ;;
        *)
            echo "Error: unknown argument: ${arg}" >&2
            exit 1
            ;;
    esac
done

if [[ ! -f "${TASKS_FILE}" ]]; then
    echo "Error: task list not found: ${TASKS_FILE}" >&2
    exit 1
fi

mkdir -p "${OUTDIR}"

# If the user requested docker cleanup, then run it every
# DOCKER_CLEANUP_INTERVAL seconds, making sure to kill the process when this
# script exits.
if [[ "${DOCKER_CLEANUP}" -eq 1 ]]; then
    bash "${CLEANUP_SCRIPT}" "${DOCKER_CLEANUP_INTERVAL}" &
    CLEANUP_PID=$!
    trap 'kill "${CLEANUP_PID}" 2>/dev/null' EXIT
fi

parallel \
    -j 4 \
    -a "${TASKS_FILE}" \
    --results "${OUTDIR}/job{#}.out" \
    --joblog "${JOBLOG}" \
    --resume
