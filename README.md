# Distributed Emergency Resource Matching System

A distributed application that accepts emergency requests, registers resources, and matches requests to nearby available resources. Versioned storage and compare-and-set updates coordinate concurrent matching, while RabbitMQ processes matching and release events asynchronously.

## Architecture

- Three Flask microservices behind nginx: requests, resources, and matching.
- One coordinator with three shards, each backed by two SQLite storage nodes.
- One RabbitMQ instance for durable matching and release queues.
- Docker Compose for local development.
- Kubernetes on Minikube as the demo deployment target.

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

The full process integration suite requires Linux. On Linux, run
`.venv/bin/python -m pytest tests -q`. On any supported Docker host, run it in
a Linux container (the same command works in bash and PowerShell):

```text
docker run --rm --mount "type=bind,source=.,target=/app,readonly" -w /app python:3.12-slim sh -c "pip install -r requirements-dev.txt && PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q -p no:cacheprovider"
```
