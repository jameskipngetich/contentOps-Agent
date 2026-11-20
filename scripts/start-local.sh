#!/usr/bin/env bash
# Build and start the local Docker Compose environment, then wait until it is healthy.
#
# Usage: scripts/start-local.sh [--monitoring]

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
    cat <<'USAGE'
Usage: start-local.sh [--monitoring]

Builds the image and starts the app (and Prometheus with --monitoring).
Waits up to 90 seconds for GET /health, then prints the service URLs.
USAGE
}

with_monitoring=0
for arg in "$@"; do
    case "${arg}" in
        --monitoring) with_monitoring=1 ;;
        -h | --help)
            usage
            exit 0
            ;;
        *) die "unknown argument: ${arg} (use -h)" ;;
    esac
done

require_cmd docker
docker compose version >/dev/null 2>&1 || die "docker compose plugin is required"

compose_args=(-f "${REPO_ROOT}/docker-compose.yml")
if [[ "${with_monitoring}" -eq 1 ]]; then
    compose_args+=(--profile monitoring)
fi

log "building and starting containers"
(cd "${REPO_ROOT}" && docker compose "${compose_args[@]}" up -d --build) ||
    die "docker compose up failed"

log "waiting for ${APP_URL}/health"
for _ in $(seq 1 45); do
    if curl -fsS --max-time 2 "${APP_URL}/health" >/dev/null 2>&1; then
        log "service is healthy"
        cat <<INFO

Application: ${APP_URL}
Swagger UI:  ${APP_URL}/docs
Metrics:     ${APP_URL}/metrics
INFO
        if [[ "${with_monitoring}" -eq 1 ]]; then
            printf 'Prometheus:  http://localhost:9090\n'
        fi
        printf 'Logs:        scripts/view-logs.sh\n'
        exit 0
    fi
    sleep 2
done

(cd "${REPO_ROOT}" && docker compose "${compose_args[@]}" ps) || true
die "service did not become healthy in 90s; check scripts/view-logs.sh"
