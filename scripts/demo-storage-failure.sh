#!/usr/bin/env bash
# Storage failure and recovery on Kubernetes (spec §10: "Storage Failure" and
# "Node Recovery"). Shows the cluster before, during and after a node outage:
#
#   1. Before:  every shard has a primary and an in-sync replica.
#   2. Failure: the shard's primary StatefulSet is scaled to 0. The coordinator
#               marks it DOWN, promotes the replica, and reads and writes keep
#               working.
#   3. Recovery: the StatefulSet is scaled back to 1. The node syncs the writes
#               it missed and rejoins as the replica (the roles have swapped),
#               which is shown by reading a key straight from that node.
#
# Run after scripts/k8s-up.sh. Exits non-zero if any step does not happen.
set -euo pipefail

NAMESPACE="${NAMESPACE:-emergency}"
PORT="${PORT:-18008}"
API="http://localhost:$PORT"
# Both keys live on shard 1 (MD5 of the key, mod 3).
KEY="request:11111111-1111-4111-8111-111111111111"
OUTAGE_KEY="request:00000004-0000-4000-8000-000000000004"
SHARD=1

kubectl -n "$NAMESPACE" port-forward svc/coordinator "$PORT:8000" >/dev/null 2>&1 &
forward=$!
trap 'kill $forward 2>/dev/null || true' EXIT

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

cluster_table() {
  curl -fsS "$API/cluster" | python3 -c '
import json, sys
for shard in json.load(sys.stdin)["shards"]:
    print("  shard %s  primary=%s  in_sync=%s" % (shard["shard"], shard["primary"], shard["in_sync"]))
    for node in shard["nodes"]:
        print("      %-6s %-8s %-8s keys=%s" % (node["node_id"], node["state"], node["role"], node["keys"]))
'
}

shard_field() {  # shard_field <python expression on `s`>
  curl -fsS "$API/cluster" | python3 -c "
import json, sys
s = next(s for s in json.load(sys.stdin)['shards'] if s['shard'] == $SHARD)
print($1)"
}

wait_for() {  # wait_for <seconds> <description> <command...>
  local seconds=$1 what=$2
  shift 2
  for _ in $(seq 1 "$seconds"); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "error: timed out after ${seconds}s waiting for: $what" >&2
  cluster_table >&2 || true
  exit 1
}

write() {  # write <key> <json value>
  local body
  body=$(python3 -c 'import json, sys; print(json.dumps({"value": sys.argv[1]}))' "$2")
  curl -fsS -X PUT "$API/data/$1" -H 'Content-Type: application/json' -d "$body"
  echo
}

both_in_sync() { [ "$(shard_field 'len(s["in_sync"])')" = 2 ]; }
primary_is_not() { [ "$(shard_field 's["primary"]')" != "$1" ]; }
node_in_sync() { shard_field '" ".join(s["in_sync"])' | grep -qw "$1"; }

wait_for 30 "the coordinator to answer" curl -fsS "$API/healthz"
wait_for 60 "shard $SHARD to have two in-sync nodes" both_in_sync

step "1. Before the failure"
cluster_table
primary=$(shard_field 's["primary"]')
echo "  shard $SHARD primary is $primary; writing $KEY through the coordinator:"
write "$KEY" '{"type":"water","phase":"before failure"}'

step "2. Failure: scaling storage-$primary to 0"
kubectl -n "$NAMESPACE" scale statefulset "storage-$primary" --replicas=0 >/dev/null
started=$(date +%s)
wait_for 30 "$primary to be marked DOWN and replaced" primary_is_not "$primary"
echo "  detected and failed over after $(( $(date +%s) - started ))s"
cluster_table
echo "  read during the outage:"
curl -fsS "$API/data/$KEY"; echo
echo "  writes during the outage:"
write "$KEY" '{"type":"water","phase":"updated during outage"}'
write "$OUTAGE_KEY" '{"type":"food","phase":"created during outage"}'

step "3. Recovery: scaling storage-$primary back to 1"
kubectl -n "$NAMESPACE" scale statefulset "storage-$primary" --replicas=1 >/dev/null
kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/storage-$primary-0" --timeout=120s >/dev/null
wait_for 60 "$primary to finish syncing and rejoin" node_in_sync "$primary"
cluster_table
echo "  $primary is back as the replica. Reading straight from its own storage (not via the coordinator):"
for key in "$KEY" "$OUTAGE_KEY"; do
  kubectl -n "$NAMESPACE" exec "storage-$primary-0" -- python -c \
    "import urllib.request, sys; print('  ', urllib.request.urlopen(sys.argv[1]).read().decode())" \
    "http://localhost:8080/internal/data/$key"
done

step "Done: service continued through the failure, and the node recovered the writes it missed"
