# Distributed Emergency Resource Matching System

A distributed system that connects people who need emergency help (water, food, medicine, transportation, shelter) with available volunteers and resources. Requests are stored in a sharded, replicated key-value store, published to RabbitMQ, and matched asynchronously to the nearest available resource of the right type. Everything runs as containers on Kubernetes.

Cloud Computing course project by Pranav Goyal and Aksha Alkesh Jain.

## Status

Phase 0 — setup and contracts. See the roadmap in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Architecture at a glance

- **3 microservices:** `request-service`, `resource-service`, `matching-service` (Python, Flask)
- **Storage layer:** 1 coordinator, 3 shards, each with a primary and a replica (SQLite per node)
- **Messaging:** 1 RabbitMQ instance between request processing and matching
- **Deployment:** Docker Compose for local development, Kubernetes (Minikube) for the demo

## Documents

- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — design, ownership, roadmap
- [docs/VIVA_NOTES.md](docs/VIVA_NOTES.md) — talking points for the demo and viva

Setup, run and test instructions will be added as the components land.
