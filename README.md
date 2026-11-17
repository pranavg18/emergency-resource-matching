# Distributed Emergency Resource Matching System

Cloud Computing course project by Pranav Goyal and Aksha Alkesh Jain.

## Scope

The `aksha` branch adds the application layer for review: request/resource APIs,
matching and release logic, RabbitMQ processing, a storage adapter, and CLI/load
tools. It retains Pranav's storage CRUD/CAS/scan and coordinator registry.

**The combined system is not yet ready for the submission demo.** Pranav's
coordinator does not yet expose `/data` routing. Application tests can use
`FakeStorage`, but live request/resource operations need that coordinator API.
Replication, failover, recovery sync, and compatible Kubernetes deployment are
also pending. A second storage node remains `SYNCING` until recovery sync exists.

The readiness cutoff is **Wednesday, 18 November 2026**, ahead of submission on
**Thursday, 19 November**. See [readiness and review gates](docs/READINESS.md).

## Run

Requires Python 3.12 and Docker Desktop with Linux containers. From this directory:

```powershell
Copy-Item .env.example .env
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://localhost:8088/healthz
Invoke-RestMethod http://localhost:8008/cluster
```

Compose uses the separate `emergency-day1` project and ports 8088, 8008 and 15678,
so it can coexist with the full local prototype. RabbitMQ management is at
http://localhost:15678 (local development login: emergency / local-dev).
The gateway's `/healthz` confirms that nginx responds; it does not prove that the
full matching path works. Storage uses six persistent named volumes.
`docker compose down` stops the stack and preserves data.

## Verify

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m pytest tests/unit -q
docker compose config --quiet
```

The unit tests exercise application behavior in isolation. Pranav's HTTP process
integration tests require Linux, Gunicorn, and Unix process signals; GitHub Actions
runs `python -m pytest tests -q` on Ubuntu. Neither passing unit tests nor that
process suite establishes full Compose/Kubernetes acceptance. Live matching and
failure/recovery evidence must be collected after the remaining storage work lands.

## Client usage after coordinator routing lands

These commands require the complete application-to-storage path:

```powershell
.\.venv\Scripts\python -m client.cli --url http://localhost:8088 register-resource --type water --lat 28.61 --lon 77.20 --availability 50
.\.venv\Scripts\python -m client.cli --url http://localhost:8088 create-request --type water --lat 28.62 --lon 77.21 --details "Drinking water needed"
.\.venv\Scripts\python -m client.cli --url http://localhost:8088 get-request REQUEST_ID
.\.venv\Scripts\python -m client.cli --url http://localhost:8088 match REQUEST_ID
.\.venv\Scripts\python -m client.cli --url http://localhost:8088 request-status REQUEST_ID --status IN_PROGRESS
.\.venv\Scripts\python -m client.cli --url http://localhost:8088 request-status REQUEST_ID --status COMPLETED
.\.venv\Scripts\python -m client.load --url http://localhost:8088 --count 50 --workers 16
# Run after the matching queue drains:
.\.venv\Scripts\python -m client.check --coordinator http://localhost:8008
```

The storage boundary carries opaque JSON strings in each record's `value` field.
The application adapter serializes outgoing objects and decodes incoming values;
storage preserves those strings exactly. Keep this contract consistent across
coordinator routing, clients, tests, and deployment changes.

## Review and documentation

Pranav's storage PR and Aksha's application PR are reviewed separately. Merge the
approved storage PR first, retarget the stacked application PR from `pranav` to
`main`, then require Pranav's review and passing checks before merging it. Do not
merge the application PR automatically.

- [Readiness checklist and ownership](docs/READINESS.md)
- [Application API and coordinator handoff contract](docs/API.md)
- [Implementation plan](IMPLEMENTATION_PLAN.md)
- [Viva notes](docs/VIVA_NOTES.md)

The full local prototype at `codex/day-2-implementation` (`523e766`) is a reference
implementation. Its earlier validation does not establish that the separately
reviewed branches work together.
