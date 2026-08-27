# `make` is the only entry point for quality gates: CI calls these targets, so
# "passed locally" and "passed in CI" mean the same thing (CLAUDE.md).

.DEFAULT_GOAL := help
SHELL := /bin/bash

PYTHON  ?= python
PYTEST  ?= $(PYTHON) -m pytest
RUFF    ?= $(PYTHON) -m ruff
MYPY    ?= $(PYTHON) -m mypy

.PHONY: help sync fmt fmt-check lint typecheck test test-integration eval-judge check clean

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
