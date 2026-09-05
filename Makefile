# `make` is the only entry point for quality gates: CI calls these targets, so
# "passed locally" and "passed in CI" mean the same thing (CLAUDE.md).

.DEFAULT_GOAL := help
SHELL := /bin/bash

PYTHON  ?= python
PYTEST  ?= $(PYTHON) -m pytest
RUFF    ?= $(PYTHON) -m ruff
MYPY    ?= $(PYTHON) -m mypy

.PHONY: help sync fmt fmt-check lint typecheck test test-architecture test-integration eval-judge check clean

help: ## List the available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

sync: ## Install dependencies into ./.venv from uv.lock
	uv sync --locked

fmt: ## Format the tree and apply the safe lint fixes
	$(RUFF) format .
	$(RUFF) check --fix .

fmt-check: ## Fail if the tree is not formatted
	$(RUFF) format --check .

lint: fmt-check ## Ruff: format check and lint rules
	$(RUFF) check .

typecheck: ## mypy in strict mode
	$(MYPY)

test: ## Unit and architecture tests; no containers required
	$(PYTEST) -m "not integration"

# Section 21.4 runs these before everything: cheapest to run, costliest to
# regress. `test_graph_reducers` joined them in the PR that introduced the
# graph state; `test_graph_topology` joins in the PR that introduces the root
# graph — a topology test over no topology proves nothing.
test-architecture: ## The architecture guardrails alone (spec 21.4)
	$(PYTEST) tests/test_channel_isolation.py tests/test_graph_reducers.py

test-integration: ## Tests that need Postgres, Redis or Qdrant (spec 21.4)
	$(PYTEST) -m integration

eval-judge: ## LLM-as-judge round (spec 21.2); reports if credentials are absent
	$(PYTHON) -m evals.run_judge

check: lint typecheck test ## Everything CI blocks on, in CI order

clean: ## Remove caches
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

# --------------------------------------------------------------------------- #
# Local infrastructure (spec 3.1). The base file is the production topology;
# the dev override is the only thing that opens a port to the host.
# --------------------------------------------------------------------------- #

COMPOSE     ?= docker compose
COMPOSE_DEV := $(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml

.PHONY: certs env compose-config up down logs ps shell test-in-worker reset

certs: ## Generate the development CA and the per-service certificates
	./scripts/gen_dev_certs.sh $(ARGS)

# Regenerating .env changes POSTGRES_PASSWORD, and Postgres only reads it on
# first boot — an existing volume keeps the old one. `make reset` is the way out.
env: ## Create .env from the template, generating the local credentials
	@$(PYTHON) -m scripts.init_dev_env $(ARGS)

compose-config: ## Validate the combined compose configuration
	$(COMPOSE_DEV) config --quiet

up: certs env ## Bring the local stack up and wait for every service to be healthy
	$(COMPOSE_DEV) up --wait

down: ## Stop the stack, keeping the volumes
	$(COMPOSE_DEV) down

ps: ## Show the state of the local stack
	$(COMPOSE_DEV) ps

logs: ## Follow the logs of the local stack
	$(COMPOSE_DEV) logs -f

test-in-worker: ## Run the suite inside the worker, against the real services
	$(COMPOSE_DEV) run --rm worker pytest

reset: ## Destroy the local volumes and rebuild the stack from scratch
	@echo 'This deletes the local Postgres, Redis and Qdrant data.'
	$(COMPOSE_DEV) down --volumes
	$(MAKE) up

.PHONY: migrate migrate-down revision bootstrap

# Through the worker, not on the host: the generated migration URL names
# `postgres` and `/certs/ca.crt`, which resolve inside the compose network and
# nowhere else. The owner DSN is passed to this one-off container only — the
# long-running services must never hold it (spec 19.1).
# Read from .env and passed to this one-off container only. `-e VAR` without a
# value forwards the *host's* environment, which does not have it — and would
# override the value with nothing.
OWNER_DSN = $(shell sed -n 's/^MIGRATION_DATABASE_URL=//p' .env)

migrate: ## Bring the database to head (runs as the owner principal)
	@test -n "$(OWNER_DSN)" || { echo 'MIGRATION_DATABASE_URL missing from .env; run `make env`.'; exit 1; }
	$(COMPOSE_DEV) run --rm -e MIGRATION_DATABASE_URL="$(OWNER_DSN)" worker \
		python -m alembic upgrade head

# No default target. `downgrade` drops every table of section 5.2, and the only
# revision is the initial one, so pointing this at the application database
# erases every local workout. It takes an explicit DSN, and says so.
migrate-down: ## Roll back one revision. Requires DSN=<disposable database>
	@test -n "$(DSN)" || { 		echo 'migrate-down needs an explicit disposable database:'; 		echo '  make migrate-down DSN=postgresql+asyncpg://.../scratch?sslmode=verify-full&sslrootcert=/certs/ca.crt'; 		echo 'It drops every table of spec 5.2 — never point it at the application database.'; 		exit 1; 	}
	$(COMPOSE_DEV) run --rm -e MIGRATION_DATABASE_URL="$(DSN)" worker 		python -m alembic downgrade -1

# On the host: Alembic writes the new file, and the worker mounts src/ read-only.
revision: ## Create a new migration: make revision M="what it does"
	$(PYTHON) -m alembic revision -m "$(M)"

# Local-dev only, like every other target in this file: it runs through
# COMPOSE_DEV (both compose files), so the Telegram reconciliation step reads
# TELEGRAM_MODE from .env — the dev override's value, not whatever a real,
# separately-deployed production topology (docker-compose.yml alone,
# TELEGRAM_MODE hardcoded to webhook) happens to be running. Pointing this at
# a real deployment's database while .env says polling calls deleteWebhook
# against that deployment's live bot and stops its webhook traffic (S02-T08
# review). A real production bootstrap runs scripts.bootstrap directly, with
# that deployment's own environment — never through this target.
bootstrap: ## Migrate and set up the LangGraph tables. Idempotent. Local dev only.
	$(COMPOSE_DEV) run --rm -e MIGRATION_DATABASE_URL="$(OWNER_DSN)" worker \
		python -m scripts.bootstrap
