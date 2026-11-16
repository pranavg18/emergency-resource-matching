# Distributed Emergency Resource Matching System

Day 1 setup for Aksha, based on Pranav's storage branch at `0869818`.

## Scope

Shared request/resource validation, status-transition rules, JSON HTTP logging,
three Flask service skeletons with Dockerfiles, Compose and CI. Pranav's environment
configuration, storage CRUD/CAS/scan, registration and coordinator registry are included.
Business endpoints, coordinator routing, RabbitMQ matching, replication and recovery
are later milestones. A second node remains SYNCING until recovery sync is implemented.
Storage values in Pranav's API are JSON-encoded strings; the future storage client must
serialize/deserialize them. Public request/resource/match endpoints currently return 404.

## Run

Requires Docker Desktop with Linux containers. From this directory:

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
Each Python container exposes /healthz and /readyz internally. Storage uses six
persistent named volumes. `docker compose down` stops the stack and preserves data.

## Verify

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m pytest tests/unit -q
```

The existing process integration tests require Linux (gunicorn); GitHub Actions runs
all tests on Ubuntu. Use `make up`, `make down`, `make lint`, `make test` where Make
is available. See IMPLEMENTATION_PLAN.md for the roadmap and docs/VIVA_NOTES.md.
