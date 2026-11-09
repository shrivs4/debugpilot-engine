.PHONY: help env models up down logs ps test test-api test-web web-install web-dev reset

help:
	@echo "make env         Create .env from .env.example (if missing)"
	@echo "make models      Pull Ollama models that are not installed yet"
	@echo "make up          Build and start postgres, api and web"
	@echo "make down        Stop the stack (data is kept)"
	@echo "make logs        Follow api and web logs"
	@echo "make test        Run backend (in the api container) and frontend tests"
	@echo "make reset       Stop the stack and DELETE database + workspace volumes"

env:
	@test -f .env && echo ".env already exists" || (cp .env.example .env && echo "Created .env")

models:
	./scripts/ensure-ollama-models.sh

up:
	docker compose up --build -d
	@echo "Dashboard: http://localhost:$${WEB_HOST_PORT:-5173}"

down:
	docker compose down

logs:
	docker compose logs -f api web

ps:
	docker compose ps

test: test-api test-web

test-api:
	docker compose exec api pytest -q

web-install:
	cd apps/web && npm ci

test-web:
	cd apps/web && npm test

web-dev:
	cd apps/web && npm run dev

reset:
	docker compose down -v
