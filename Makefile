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
