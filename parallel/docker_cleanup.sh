#!/bin/bash
#
# docker_cleanup.sh
#
# Periodically prunes unused Docker resources (containers, images, networks,
# build cache) to prevent disk exhaustion during long-running batch jobs.
#
# Usage:
#   ./docker_cleanup.sh [interval_seconds] [--volumes]
#
# Examples:
#   ./docker_cleanup.sh              # runs every 300s, no volume pruning
#   ./docker_cleanup.sh 60           # runs every 60s
#   ./docker_cleanup.sh 60 --volumes # also prunes unused volumes (data loss risk)
#
# Run this in a separate terminal/tmux pane alongside your `parallel` job:
#   ./docker_cleanup.sh 120 &
#   CLEANUP_PID=$!
#   parallel ...
#   kill "${CLEANUP_PID}"

set -euo pipefail

INTERVAL="${1:-300}"
PRUNE_VOLUMES=false
if [[ "${2:-}" == "--volumes" ]]; then
    PRUNE_VOLUMES=true
fi

LOG_PREFIX="[docker_cleanup]"

cleanup_once() {
    local before after reclaimed
    before=$(df --output=avail -B1 / | tail -1)

    echo "${LOG_PREFIX} $(date '+%Y-%m-%d %H:%M:%S') - starting prune"

    docker container prune -f >/dev/null

    if [[ "${PRUNE_VOLUMES}" == true ]]; then
        docker system prune -a -f --volumes >/dev/null
    else
        docker system prune -a -f >/dev/null
    fi

    after=$(df --output=avail -B1 / | tail -1)
    reclaimed=$(( (after - before) / 1024 / 1024 ))

    echo "${LOG_PREFIX} $(date '+%Y-%m-%d %H:%M:%S') - done, freed ~${reclaimed}MB, avail: $(df -h / | tail -1 | awk '{print $4}')"
}

echo "${LOG_PREFIX} starting, interval=${INTERVAL}s, prune_volumes=${PRUNE_VOLUMES}"

while true; do
    cleanup_once
    sleep "${INTERVAL}"
done
