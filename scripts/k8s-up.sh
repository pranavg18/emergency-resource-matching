#!/usr/bin/env bash
# Build the images, load them into minikube, and deploy to the `emergency`
# namespace. Re-run after code changes: it reloads images and restarts pods.
#
#   scripts/k8s-up.sh               # images tagged :dev
#   TAG=review-v2 scripts/k8s-up.sh # any other tag; the manifests follow it
#
# Any failure (build, load, apply, or a workload that never becomes ready)
# stops the script with a non-zero exit and prints the pods and recent events.
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${TAG:-dev}"
NAMESPACE=emergency
PROFILE="${MINIKUBE_PROFILE:-minikube}"

if [[ ! "$TAG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "error: TAG must be a valid image tag (letters, digits, '.', '_', '-'), got '$TAG'" >&2
  exit 2
fi

diagnose() {
  local status=$?
  echo >&2
  echo "error: deployment failed (exit $status). Current state:" >&2
  kubectl -n "$NAMESPACE" get pods -o wide >&2 || true
  kubectl -n "$NAMESPACE" get events --sort-by=.lastTimestamp 2>/dev/null | tail -n 20 >&2 || true
  exit "$status"
}
trap diagnose ERR

if [ "$(kubectl config current-context)" != "$PROFILE" ]; then
  echo "error: select kubectl context $PROFILE before deploying (MINIKUBE_PROFILE=$PROFILE)." >&2
  exit 2
fi

if ! kubectl -n "$NAMESPACE" get secret rabbitmq-credentials >/dev/null 2>&1; then
  echo "error: create broker credentials first: python scripts/create-rabbitmq-secret.py" >&2
  exit 2
fi

build() {  # build <image> <dockerfile> <context>
  echo "==> building $1:$TAG"
  docker build -q -t "$1:$TAG" -f "$2" "$3" >/dev/null
  echo "==> loading $1:$TAG into minikube"
  minikube -p "$PROFILE" image load --overwrite=true "$1:$TAG"
}

build emergency/coordinator coordinator/Dockerfile coordinator
build emergency/storage-node storage_node/Dockerfile storage_node
for service in request_service resource_service matching_service; do
  if [ -f "services/$service/Dockerfile" ]; then
    build "emergency/${service//_/-}" "services/$service/Dockerfile" .
  fi
done

# The manifests reference emergency/*:dev; render them with the tag just built,
# so a cluster never runs an image other than the one this script loaded.
rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
kubectl kustomize deploy/k8s \
  | sed -E "s#^([[:space:]]*image:[[:space:]]*emergency/[a-z0-9-]+):dev[[:space:]]*\$#\\1:${TAG}#" \
  > "$rendered"
if grep -En "image:[[:space:]]*emergency/[a-z0-9-]+:" "$rendered" | grep -v ":${TAG}\$" >&2; then
  echo "error: the images above were not retagged to :$TAG" >&2
  exit 1
fi

echo "==> applying manifests (images :$TAG)"
kubectl apply -f "$rendered"

echo "==> restarting workloads so they pick up the new images"
kubectl -n "$NAMESPACE" rollout restart deployment,statefulset >/dev/null
kubectl -n "$NAMESPACE" rollout status deployment --timeout=180s
kubectl -n "$NAMESPACE" rollout status statefulset --timeout=180s

echo
echo "Done. Coordinator view of the cluster:"
echo "  kubectl -n $NAMESPACE port-forward svc/coordinator 8008:8000"
echo "  curl localhost:8008/cluster"
