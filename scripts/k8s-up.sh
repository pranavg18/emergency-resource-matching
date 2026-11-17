#!/usr/bin/env bash
# Build the images, load them into minikube, and deploy to the `emergency`
# namespace. Re-run after code changes: it reloads images and restarts pods.
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${TAG:-dev}"

build() {  # build <image> <dockerfile> <context>
  echo "==> building $1:$TAG"
  docker build -q -t "$1:$TAG" -f "$2" "$3" >/dev/null
  echo "==> loading $1:$TAG into minikube"
  minikube image load --overwrite=true "$1:$TAG"
}

build emergency/coordinator coordinator/Dockerfile coordinator
build emergency/storage-node storage_node/Dockerfile storage_node
for service in request_service resource_service matching_service; do
  if [ -f "services/$service/Dockerfile" ]; then
    build "emergency/${service//_/-}" "services/$service/Dockerfile" .
  fi
done

echo "==> applying manifests"
kubectl apply -k deploy/k8s

echo "==> restarting workloads so they pick up the new images"
kubectl -n emergency rollout restart deployment,statefulset >/dev/null
kubectl -n emergency rollout status deployment --timeout=180s
kubectl -n emergency rollout status statefulset --timeout=180s 2>/dev/null || true

echo
echo "Done. Coordinator view of the cluster:"
echo "  kubectl -n emergency port-forward svc/coordinator 8008:8000"
echo "  curl localhost:8008/cluster"
