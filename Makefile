.PHONY: up down logs test lint
up:
	docker compose up -d --build
down:
	docker compose down
logs:
	docker compose logs -f --tail 30
test:
	python -m pytest tests/unit -q
lint:
	python -m ruff check .
