.PHONY: up dev down logs test

up:
	docker compose up --build

dev: up

down:
	docker compose down

logs:
	docker compose logs -f api worker web

test:
	.venv/bin/python -m pytest -q
	.venv/bin/ruff check app tests
	cd frontend && pnpm test && pnpm build
