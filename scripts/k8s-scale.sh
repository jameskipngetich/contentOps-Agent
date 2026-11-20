#!/usr/bin/env bash
# Scale the deployment and wait for the new replicas to become available.
#
# Usage: scripts/k8s-scale.sh REPLICAS

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if [[ $# -ne 1 || "$1" == "-h" ]]; then
    cat <<'USAGE'
Usage: k8s-scale.sh REPLICAS

REPLICAS must be an integer between 0 and 10. Scaling to 0 stops the service and
is confirmed explicitly.
USAGE
    [[ $# -eq 1 ]] && exit 0
    exit 2
fi

replicas="$1"
[[ "${replicas}" =~ ^[0-9]+$ ]] || die "REPLICAS must be a positive integer, got '${replicas}'"
((replicas <= 10)) || die "refusing to scale above 10 replicas on a local cluster"

require_cmd kubectl
kubectl cluster-info >/dev/null 2>&1 || die "kubectl cannot reach a cluster"

if [[ "${replicas}" -eq 0 ]]; then
    confirm "Scaling to 0 stops the service. Continue?"
fi

log "scaling ${DEPLOYMENT} to ${replicas} replica(s)"
kubectl -n "${NAMESPACE}" scale "deployment/${DEPLOYMENT}" --replicas="${replicas}" ||
    die "scale command failed"

if [[ "${replicas}" -gt 0 ]]; then
    kubectl -n "${NAMESPACE}" rollout status "deployment/${DEPLOYMENT}" --timeout=120s ||
        die "replicas did not become ready; run scripts/k8s-status.sh"
else
    kubectl -n "${NAMESPACE}" get pods
fi

log "deployment is at ${replicas} replica(s)"
