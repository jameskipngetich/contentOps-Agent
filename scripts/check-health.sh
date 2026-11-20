#!/usr/bin/env bash
# Check the service probes and report a summary.
#
# Usage: scripts/check-health.sh [-u BASE_URL] [--metrics]
# Exit:  0 healthy, 1 unhealthy or unreachable

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
    cat <<'USAGE'
Usage: check-health.sh [-u BASE_URL] [--metrics]

Calls GET /health and GET /ready, prints the dependency statuses, and exits
non-zero if either check fails. --metrics additionally checks /metrics.

Env: APP_URL (default http://localhost:8080)
USAGE
}

base_url="${APP_URL}"
check_metrics=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -u)
            base_url="$2"
            shift 2
            ;;
        --metrics)
            check_metrics=1
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *) die "unknown argument: $1 (use -h)" ;;
    esac
done

require_cmd curl

failed=0

probe() {
    local path="$1" url="${base_url}$1"
    local body
    if ! body="$(curl -fsS --max-time 5 "${url}" 2>/dev/null)"; then
        printf '%-8s FAIL  %s\n' "${path}" "${url} unreachable"
        return 1
    fi
    printf '%-8s OK    %s\n' "${path}" "${body}"
    return 0
}

probe /health || failed=1
probe /ready || failed=1

if [[ "${check_metrics}" -eq 1 ]]; then
    if curl -fsS --max-time 5 "${base_url}/metrics" | grep -q '^contentops_http_requests_total'; then
        printf '%-8s OK    application metrics present\n' "/metrics"
    else
        printf '%-8s FAIL  application metrics missing\n' "/metrics"
        failed=1
    fi
fi

if [[ "${failed}" -ne 0 ]]; then
    log "service is not healthy"
    exit 1
fi
log "service is healthy"
