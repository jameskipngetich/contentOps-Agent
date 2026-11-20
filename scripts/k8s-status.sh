#!/usr/bin/env bash
# Print the state of the deployment, pods, service and nodes.
#
# Usage: scripts/k8s-status.sh

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_cmd kubectl
kubectl cluster-info >/dev/null 2>&1 || die "kubectl cannot reach a cluster"

section() { printf '\n=== %s ===\n' "$1"; }

section "nodes"
kubectl get nodes -o wide

section "pods (namespace ${NAMESPACE})"
kubectl -n "${NAMESPACE}" get pods -o wide

section "deployments"
kubectl -n "${NAMESPACE}" get deployments

section "replicasets"
kubectl -n "${NAMESPACE}" get replicasets

section "services and endpoints"
kubectl -n "${NAMESPACE}" get services
kubectl -n "${NAMESPACE}" get endpointslices -l "kubernetes.io/service-name=${DEPLOYMENT}" 2>/dev/null ||
    kubectl -n "${NAMESPACE}" get endpoints "${DEPLOYMENT}" 2>/dev/null ||
    warn "no endpoints object found"

section "persistent volume claim"
kubectl -n "${NAMESPACE}" get pvc

section "recent events"
kubectl -n "${NAMESPACE}" get events --sort-by=.lastTimestamp | tail -n 15

if kubectl get --raw /apis/metrics.k8s.io/v1beta1 >/dev/null 2>&1; then
    section "resource usage"
    kubectl -n "${NAMESPACE}" top pods || warn "metrics-server returned no data yet"
else
    warn "metrics-server is not available; skipping 'kubectl top'"
fi
