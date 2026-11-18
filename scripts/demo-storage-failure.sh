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
# Safe to run on a cluster the application is using: its two demo records are
# valid closed requests under keys unique to the run, deleted on exit, and a
# node it scaled down is scaled back up even if the script fails midway.
set -euo pipefail

NAMESPACE="${NAMESPACE:-emergency}"
PORT="${PORT:-18008}"
API="http://localhost:$PORT"
SHARD="${SHARD:-1}"

# Checked before anything else runs: the key search below only ends for a
# shard that exists, so any other value would make it loop forever.
if [[ ! "$SHARD" =~ ^[012]$ ]]; then
  echo "error: SHARD must be 0, 1 or 2 (the cluster has three shards), got '$SHARD'" >&2
  exit 2
fi

# Two fresh keys owned by $SHARD (MD5 of the key, mod 3), unique to this run.
# They hold complete request records with status CANCELLED: valid for every
# service that scans requests, and ignored by the matcher, which only acts on
# open requests. Both are deleted when the script exits, however it exits.
read -r KEY OUTAGE_KEY < <(python3 -c '
import hashlib, sys, uuid
keys = []
while len(keys) < 2:
    key = f"request:{uuid.uuid4()}"
    if int.from_bytes(hashlib.md5(key.encode()).digest(), "big") % 3 == int(sys.argv[1]):
        keys.append(key)
print(*keys)' "$SHARD")
if [ -z "${OUTAGE_KEY:-}" ]; then
  echo "error: could not generate demo keys (is python3 installed?)" >&2
  exit 1
fi

scaled_down=""
cleanup() {
  local status=$?
  if [ -n "$scaled_down" ]; then
    echo "cleanup: scaling storage-$scaled_down back to 1" >&2
    kubectl -n "$NAMESPACE" scale statefulset "storage-$scaled_down" --replicas=1 >/dev/null 2>&1 || true
  fi
  for key in "$KEY" "$OUTAGE_KEY"; do
    curl -fsS -o /dev/null -X DELETE "$API/data/$key" 2>/dev/null || true
  done
  kill "$forward" 2>/dev/null || true
  exit "$status"
}

kubectl -n "$NAMESPACE" port-forward svc/coordinator "$PORT:8000" >/dev/null 2>&1 &
forward=$!
trap cleanup EXIT

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

write() {  # write <key> <type> <details>: a complete, closed request record
  local body
  body=$(python3 -c '
import json, sys
from datetime import datetime, timezone
key, kind, details = sys.argv[1:4]
now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
record = {
    "id": key.split(":", 1)[1], "type": kind,
    "location": {"lat": 28.61, "lon": 77.2, "label": "storage-failure demo"},
    "details": details, "status": "CANCELLED", "queued": True,
    "candidate_resource_id": None, "assigned_resource_id": None, "match_attempt": 0,
    "created_at": now, "updated_at": now,
}
print(json.dumps({"value": json.dumps(record, separators=(",", ":"))}))' "$1" "$2" "$3")
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
write "$KEY" water "written before the failure"

step "2. Failure: scaling storage-$primary to 0"
scaled_down="$primary"
kubectl -n "$NAMESPACE" scale statefulset "storage-$primary" --replicas=0 >/dev/null
started=$(date +%s)
wait_for 30 "$primary to be marked DOWN and replaced" primary_is_not "$primary"
echo "  detected and failed over after $(( $(date +%s) - started ))s"
cluster_table
echo "  read during the outage:"
curl -fsS "$API/data/$KEY"; echo
echo "  writes during the outage:"
write "$KEY" water "updated during the outage"
write "$OUTAGE_KEY" food "created during the outage"

step "3. Recovery: scaling storage-$primary back to 1"
kubectl -n "$NAMESPACE" scale statefulset "storage-$primary" --replicas=1 >/dev/null
scaled_down=""
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
echo "  (the two demo records are deleted on exit)"
