# Distributed Emergency Resource Matching System

A distributed application that accepts emergency requests, registers resources, and matches requests to nearby available resources. Versioned storage and compare-and-set updates coordinate concurrent matching, while RabbitMQ processes matching and release events asynchronously.

## Architecture

- Three Flask microservices: requests, resources, and matching.
- One coordinator with three shards, each backed by two SQLite storage nodes.
- One RabbitMQ instance for durable matching and release queues.
- Docker Compose with nginx for local development.
- Kubernetes on Minikube with an nginx Ingress controller for the demo.

The matcher ranks eligible resources by distance, available units, creation time
and ID. It records its candidate before reserving a unit with a versioned
compare-and-set. Request attempt numbers fence stale workers; duplicate delivery
and release are safe. A sweeper republishes requests after broker outages and
repairs abandoned claims. Storage errors retry without consuming search attempts.

## Run

Requires Python 3.12 and Docker with Linux containers. Local endpoints are
`http://localhost:8088` (API), `http://localhost:8008/cluster` (coordinator), and
`http://localhost:15678` (RabbitMQ management, local login `emergency` / `local-dev`).

macOS/Linux (bash):

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
curl http://localhost:8088/healthz
curl http://localhost:8008/cluster
```

Windows (PowerShell):

```powershell
Copy-Item .env.example .env
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://localhost:8088/healthz
Invoke-RestMethod http://localhost:8008/cluster
```

Use `docker compose logs -f --tail 30` for logs and `docker compose down` to stop
the containers while preserving data volumes.

## Test

macOS/Linux (bash):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest tests/unit -q
docker compose config --quiet
```

Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m pytest tests/unit -q
docker compose config --quiet
```

The full process integration suite runs on Linux and macOS (Gunicorn uses POSIX
signals). Run `.venv/bin/python -m pytest tests -q`. Deployment-script tests also
need `bash` and `kubectl`; pytest reports them as skipped when either is missing.
On Windows, run the full suite in a Linux container. The following works in both
bash and PowerShell; its slim image skips tests that need `kubectl`:

```text
docker run --rm --mount "type=bind,source=.,target=/app,readonly" -w /app python:3.12-slim sh -c "pip install -r requirements-dev.txt && PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q -p no:cacheprovider"
```

## Kubernetes

Requires Docker, Minikube, kubectl, Python and bash (Git Bash on Windows). From
the repository root, start an isolated local profile and its Ingress controller:

```text
minikube start -p emergency-demo --driver=docker --container-runtime=containerd --cpus=4 --memory=4096 --kubernetes-version=v1.36.1
minikube -p emergency-demo addons enable ingress
kubectl config use-context emergency-demo
python scripts/create-rabbitmq-secret.py
```

The helper creates a random broker password in `rabbitmq-credentials` only when
that Secret is absent. It does not print, save locally, or replace the password.
Keep the Secret when reusing RabbitMQ's persistent volume.

Build, load and deploy on macOS/Linux:

```bash
MINIKUBE_PROFILE=emergency-demo bash scripts/k8s-up.sh
```

On Windows PowerShell:

```powershell
$env:MINIKUBE_PROFILE = "emergency-demo"
& "C:\Program Files\Git\usr\bin\bash.exe" scripts/k8s-up.sh
```

The script checks the selected context, applies `deploy/k8s/kustomization.yaml`
in namespace `emergency`, and fails if any workload does not become ready.
Application images use `emergency/<service>:dev` and `IfNotPresent`. Set `TAG`
alongside `MINIKUBE_PROFILE` to deploy another tag. Shared storage settings come
from `storage-config`; application settings come from `application-config`.
`COORDINATOR_URL` is `http://coordinator:8000`.

All microservices listen on port 8000. Ingress forwards `/requests`, `/resources`
and `/match` without rewriting their paths. RabbitMQ and each storage node have
their own persistent volume. Readiness checks the coordinator and does not depend
on RabbitMQ, so an outage can still return a stored request with `queued: false`.

Open these forwards in **two separate terminals**; the API forward goes through
the actual Ingress controller, including on Windows with Docker's driver:

```text
kubectl --context emergency-demo -n ingress-nginx port-forward svc/ingress-nginx-controller 8088:80
```

```text
kubectl --context emergency-demo -n emergency port-forward svc/coordinator 8008:8000
```

Stop a Compose stack using those local ports before opening the forwards. Use
`kubectl --context emergency-demo -n emergency get pods,pvc,ingress` to inspect
the deployment. `minikube stop -p emergency-demo` stops it while retaining data.

## Web client (map)

A browser client for the same API the CLI uses. With the API on port 8088 and
the coordinator on 8008 (the two forwards above, or the Compose stack), run:

```text
python -m client.web
```

and open http://localhost:5050. Click the map to pick exact coordinates (or
type them), then create a request or register a resource there. Requests and
resources appear as markers coloured by status and refresh every two seconds,
so asynchronous matching shows as a dashed line from a request to its assigned
resource. Selecting a marker shows its details and actions: match a request
now, start, complete or cancel it, take a resource offline or change its
availability. A side panel shows each shard's primary and the state of every
storage node, so a storage failure and recovery can be watched live.

`client/web.py` serves the page and forwards its calls (`/api/requests`,
`/api/resources`, `/api/match`) to the API, so the browser talks to one origin
and nothing in the cluster changes; it is a client, like the CLI. The public
API has no request-listing endpoint, so the map reads the request list from the
coordinator, as `client.check` does; that path is read-only. The client only
answers on loopback hostnames, and accepts writes only as JSON from its own
page, so other sites open in the browser cannot use it. Use `--api`,
`--coordinator` and `--port` for other addresses. The map uses
[Leaflet](https://leafletjs.com) with OpenStreetMap tiles, both loaded over the
internet.

## Demo runbook

With the two forwards running, use a third terminal from the repository root
with the project dependencies installed. First show the basic lifecycle:

```text
python -m client.cli register-resource --type food --lat 12.97 --lon 77.59 --availability 2
python -m client.cli create-request --type food --lat 12.97 --lon 77.59 --details "Demo food request"
python -m client.cli get-request REQUEST_ID
python -m client.cli request-status REQUEST_ID --status IN_PROGRESS
python -m client.cli request-status REQUEST_ID --status COMPLETED
python -m client.cli list-resources --type food
python -m client.check
```

Replace `REQUEST_ID` with the returned ID and wait for `MATCHED` before advancing
its status. Completion releases the unit asynchronously. Cancellation follows
the same release path. Use `kubectl -n emergency logs -l app=matching-service
--tail=50` to show consume/ack events and correlation IDs.

Run the repeatable checks:

```text
python -m client.demo --context emergency-demo --scenario all --output artifacts/k8s-demo.json
```

- **Scaling:** 40 requests against 40 units with one and then four matcher pods.
  A 200 ms demo delay makes the worker-count difference visible. The report records
  elapsed time, throughput and claim-contention retries.
- **Concurrency:** 100 simultaneous requests against 70 units with four matcher
  pods. Expect exactly 70 matches, 30 unmatched requests, no duplicate assignments,
  nonnegative availability and a passing `client.check`.
- **Recovery:** wait for an unacknowledged delivery, then normally delete a matcher
  pod (SIGTERM) and separately kill its container through Minikube's runtime
  (verified exit code 137). Expect redelivery, one assignment and restored
  capacity after cancellation.

The command restores matcher replicas and delay settings, closes its own requests,
and retires its own resources. It preserves existing records and saves JSON evidence
under ignored `artifacts/`. Use `--scenario concurrency`, `scaling` or `recovery`
to run a subset; concurrency also works against Compose. Override `--url` and
`--coordinator` for different forwarded ports. Do not run another load generator
concurrently with these checks.

On SIGTERM, the matcher stops accepting deliveries and lets an active handler
finish and acknowledge before its owning thread closes the Pika connection. Work
that has not started, or cannot finish before the grace deadline, stays unacknowledged
for redelivery. The drain budget is 25 seconds, Gunicorn's grace is 40 seconds and
the pod/Compose grace is 45 seconds. The process regression test holds an active
handler open and verifies this ordering with the real Gunicorn signal path.

For a broker-outage demonstration, scale `statefulset/rabbitmq` to zero, create a
request, and show its `queued: false` response. Scale the StatefulSet back to one
and wait for the sweeper to publish and match it (up to roughly a minute). Do not
remove its PVC or change its Secret. Application logs retain structured retry
events while suppressing Pika's repeated connection tracebacks.

### Storage failure and node recovery

```text
SHARD=1 bash scripts/demo-storage-failure.sh
```

The script shows the cluster before, during and after a storage-node outage.
It scales the chosen shard's primary StatefulSet to zero; the coordinator marks
that node DOWN after about six seconds of missed heartbeats and promotes the
in-sync replica, and reads and writes on the shard keep working. It then scales
the node back to one: the node syncs the writes it missed, rejoins as the
replica (the roles have swapped), and the script reads those writes directly
from the recovered node's own storage. `SHARD` may be 0, 1 or 2 (default 1).
The script uses two temporary request records on that shard and deletes them on
exit, and if it stops early it scales the node back up. It forwards the
coordinator on port 18008, so it can run alongside the two forwards above.
