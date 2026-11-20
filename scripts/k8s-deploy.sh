#!/usr/bin/env bash
# Build the image, load it into a local cluster if needed, and apply the manifests.
#
# Usage: scripts/k8s-deploy.sh [--skip-build]
# Env:   IMAGE (default contentops-agent:local), KIND_CLUSTER (optional)

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
    cat <<'USAGE'
Usage: k8s-deploy.sh [--skip-build]

1. docker build -t $IMAGE .
2. kind load docker-image $IMAGE --name $KIND_CLUSTER   (when KIND_CLUSTER is set)
3. kubectl apply -k k8s/
4. kubectl set image and kubectl rollout status

Env:
  IMAGE         image reference to build/deploy (default contentops-agent:local)
  KIND_CLUSTER  kind cluster name; set this for kind, leave empty for Minikube
                (Minikube users should run: eval $(minikube docker-env) first)
USAGE
}

skip_build=0
for arg in "$@"; do
    case "${arg}" in
        --skip-build) skip_build=1 ;;
        -h | --help)
            usage
            exit 0
            ;;
        *) die "unknown argument: ${arg} (use -h)" ;;
    esac
done

require_cmd kubectl
kubectl cluster-info >/dev/null 2>&1 || die "kubectl cannot reach a cluster"

image="${IMAGE:-contentops-agent:local}"

if [[ "${skip_build}" -eq 0 ]]; then
    require_cmd docker
    log "building ${image}"
    (cd "${REPO_ROOT}" && docker build -t "${image}" .) || die "docker build failed"
fi

if [[ -n "${KIND_CLUSTER:-}" ]]; then
    require_cmd kind
    log "loading ${image} into kind cluster ${KIND_CLUSTER}"
    kind load docker-image "${image}" --name "${KIND_CLUSTER}" ||
        die "could not load the image into kind"
fi

log "applying manifests"
kubectl apply -k "${REPO_ROOT}/k8s" || die "kubectl apply failed"

log "pointing the deployment at ${image}"
kubectl -n "${NAMESPACE}" set image "deployment/${DEPLOYMENT}" "api=${image}" ||
    die "could not update the deployment image"

log "waiting for the rollout"
kubectl -n "${NAMESPACE}" rollout status "deployment/${DEPLOYMENT}" --timeout=120s ||
    die "rollout did not complete; run scripts/k8s-status.sh"

kubectl -n "${NAMESPACE}" get pods -l "app.kubernetes.io/name=${DEPLOYMENT}" -o wide
cat <<INFO

Service DNS inside the cluster: http://${DEPLOYMENT}.${NAMESPACE}.svc.cluster.local
Port-forward for local use:      kubectl -n ${NAMESPACE} port-forward svc/${DEPLOYMENT} 8080:80
INFO
