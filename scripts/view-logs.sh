#!/usr/bin/env bash
# Follow application logs from Docker Compose or Kubernetes.
#
# Usage: scripts/view-logs.sh [--k8s] [--no-follow] [--tail N]

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
    cat <<'USAGE'
Usage: view-logs.sh [--k8s] [--no-follow] [--tail N]

Docker Compose is the default source. --k8s reads logs from the pods selected by
app.kubernetes.io/name=contentops-agent.
USAGE
}

target="compose"
follow=1
tail_lines=200
while [[ $# -gt 0 ]]; do
    case "$1" in
        --k8s) target="k8s" ;;
        --no-follow) follow=0 ;;
        --tail)
            tail_lines="$2"
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *) die "unknown argument: $1 (use -h)" ;;
    esac
    shift
done

if [[ "${target}" == "k8s" ]]; then
    require_cmd kubectl
    args=(-n "${NAMESPACE}" logs -l "app.kubernetes.io/name=${DEPLOYMENT}" --tail="${tail_lines}")
    if [[ "${follow}" -eq 1 ]]; then
        args+=(--follow)
    fi
    exec kubectl "${args[@]}"
fi

require_cmd docker
args=(logs --tail="${tail_lines}")
if [[ "${follow}" -eq 1 ]]; then
    args+=(--follow)
fi
exec docker compose -f "${REPO_ROOT}/docker-compose.yml" "${args[@]}" app
