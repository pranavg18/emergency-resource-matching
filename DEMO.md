# Minikube rehearsal

Run the numbered sections in order in one Bash session from the repository root. Use a dedicated local profile for each rehearsal so previous water resources cannot match the first request. Use Git Bash with working `python` and `python3` on Windows. The Python environment needs the project dependencies. Allow local ports 8088, 8008 and 18008. Run one load generator at a time.

```bash
set -eo pipefail
git fetch origin
git switch main
git merge --ff-only origin/main
git rev-parse HEAD
export MINIKUBE_PROFILE=emergency-rehearsal-1  # use emergency-rehearsal-2 tomorrow
minikube start -p "$MINIKUBE_PROFILE" --driver=docker --container-runtime=containerd --cpus=4 --memory=4096 --kubernetes-version=v1.36.1
minikube -p "$MINIKUBE_PROFILE" addons enable ingress
kubectl config use-context "$MINIKUBE_PROFILE"
python -m pip install -r requirements.txt
python scripts/create-rabbitmq-secret.py
bash scripts/k8s-up.sh
k() { kubectl --context "$MINIKUBE_PROFILE" -n emergency "$@"; }
cli() { python -m client.cli --url http://localhost:8088 "$@"; }
export API_URL=http://localhost:8088
run=artifacts/$(date +%Y%m%d-%H%M%S)
mkdir -p "$run"
git rev-parse HEAD > "$run/commit.txt"
kubectl --context "$MINIKUBE_PROFILE" -n ingress-nginx rollout status deployment/ingress-nginx-controller
kubectl --context "$MINIKUBE_PROFILE" -n ingress-nginx port-forward svc/ingress-nginx-controller 8088:80 >"$run/ingress.log" 2>&1 & ing=$!
kpf=(kubectl --context "$MINIKUBE_PROFILE" -n emergency port-forward svc/coordinator 8008:8000)
"${kpf[@]}" >"$run/coordinator.log" 2>&1 & co=$!
trap 'kill "$ing" "$co" 2>/dev/null || true' EXIT
curl -fsS --retry 15 --retry-connrefused http://localhost:8088/resources >/dev/null
curl -fsS --retry 15 --retry-connrefused http://localhost:8008/cluster | python -m json.tool
id() { python -c 'import json,sys; print(json.load(sys.stdin)["id"])'; }
demo() { python -m client.demo --context "$MINIKUBE_PROFILE" --scenario "$1" --output "$run/$1.json"; }
```

Start with all three shards showing a primary and two in-sync nodes apiece. Keep the terminal open for the following commands.

## 1. Request creation

```bash
req=$(cli create-request --type water --lat 28.61 --lon 77.20 --details "Rehearsal water delivery" | tee "$run/request.json" | id)
cli get-request "$req"
```

Read back the same ID and location. Continue promptly so the registration arrives during the retry window.

## 2. Resource registration

```bash
res=$(cli register-resource --type water --lat 28.61 --lon 77.20 --availability 250 | tee "$run/resource.json" | id)
cli list-resources --type water
```

Find the new resource ID in the list. This resource also supplies the later load tests.

## 3. Resource matching

```bash
for i in {1..30}; do
  cli get-request "$req" > "$run/match.json"
  python -c 'import json,sys; r=json.load(open(sys.argv[1])); sys.exit(0 if r["status"]=="MATCHED" and r["assigned_resource_id"]==sys.argv[2] else 1)' "$run/match.json" "$res" && break
  sleep 1
done
cat "$run/match.json"
python -c 'import json,sys; r=json.load(open(sys.argv[1])); assert r["status"]=="MATCHED" and r["assigned_resource_id"]==sys.argv[2]' "$run/match.json" "$res"
cli list-resources --type water
```

Expect the assigned ID to equal the registered ID and availability to fall to 249. If the request exhausted its retries before registration, repeat item 1 with the resource available and check the new request.

## 4. Data distribution

```bash
python -m client.load --count 30 --output "$run/distribution.json"
curl -fsS http://localhost:8008/cluster | tee "$run/shards.json" | python -m json.tool
```

Show nonzero key counts on all three shards. Compare primary and replica counts after writes settle. Hashing distributes keys without promising equal totals between shards.

## 5. Asynchronous processing

```bash
k set env deployment/matching-service MATCH_DELAY_MS=300
k rollout status deployment/matching-service
python -m client.load --count 50 --workers 16 --output "$run/async.json" & burst=$!
for i in {1..8}; do
  k exec rabbitmq-0 -- rabbitmqctl -q list_queues name messages_ready messages_unacknowledged
  sleep 1
done
wait "$burst"
k logs -l app=matching-service --tail=25
k set env deployment/matching-service MATCH_DELAY_MS-
k rollout status deployment/matching-service
```

Show 50 accepted requests and the matcher consuming the backlog independently. Queue depth is a snapshot; repeat the queue command if deliveries are still outstanding.

## 6. Storage failure

```bash
SHARD=1 PORT=18008 bash scripts/demo-storage-failure.sh | tee "$run/storage.log"
```

One invocation performs items 6 and 7. During the failure, identify the old primary, the promoted replica, and the successful read and writes while the old node is down.

## 7. Node recovery

```bash
cat "$run/storage.log"
curl -fsS http://localhost:8008/cluster | python -m json.tool
```

Use the recovery portion of the captured output to show the outage values read directly from the returning node. It should now be an UP replica in the in-sync set. The script cleans up its two temporary records on exit.

## 8. Kubernetes pod recovery

Use main with the request-service redundancy follow-up deployed. The first check stops this step if fewer than two replicas are ready.

```bash
test "$(k get deployment request-service -o jsonpath='{.status.readyReplicas}')" -ge 2
pod=$(k get pods -l app=request-service -o jsonpath='{.items[0].metadata.name}')
python -m client.load --count 88 --rate 4 --output "$run/http-recovery.json" & load=$!
sleep 3
k delete pod "$pod" --wait=false
wait "$load"
k rollout status deployment/request-service
k get pods -l app=request-service
demo recovery
```

Expect 88 creations with zero failures and a replacement HTTP pod. The separate matcher check exercises normal deletion and a hard kill during delivery and checks redelivery, one assignment and release.

## 9. Kubernetes scaling

```bash
demo scaling
python -m client.check --coordinator http://localhost:8008
python -m json.tool "$run/scaling.json"
```

Compare elapsed time and throughput for the same 40 requests with one and four matchers. The scenario also runs 100 requests against 70 units; expect 70 matches, no duplicate assignments and a passing consistency check. Preserve the evidence directory with its commit ID.

When both presenters have finished, close the forwards and stop the profile while retaining its data:

```bash
kill "$ing" "$co" || true
trap - EXIT
minikube stop -p "$MINIKUBE_PROFILE"
```
