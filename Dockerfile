# syntax=docker/dockerfile:1
#
# One image for the three application services (spec 3.1): ingress, worker and
# scheduler differ by command, never by build. Base images are pinned by digest
# per spec 22 ("imagens fixadas por digest").

ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/venv/bin:$PATH

COPY --from=uv /uv /usr/local/bin/uv

# The unprivileged user owns nothing the application writes to at runtime:
# state lives in Postgres, Redis or Qdrant (invariant 5 of CLAUDE.md).
RUN groupadd --gid 10001 fittrack \
 && useradd --uid 10001 --gid fittrack --create-home --shell /usr/sbin/nologin fittrack

WORKDIR /app

# ---------------------------------------------------------------------------
# runtime: what production runs. Locked, no dev or test group.
# ---------------------------------------------------------------------------
FROM base AS runtime

COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
# Versioned configuration travels with the image: model tiering by role and the
# prompts, neither of which may live in Python (CLAUDE.md, invariant 4).
COPY config/ ./config/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups

USER fittrack
CMD ["python", "-m", "fittrack.worker"]

# ---------------------------------------------------------------------------
# dev: the same tree plus the test toolchain, so the suite runs in the worker
# against the real services (CLAUDE.md, path 2).
# ---------------------------------------------------------------------------
FROM base AS dev

COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
COPY config/ ./config/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

# The certificate tests drive the real `openssl` CLI. The base image happens to
# ship it as a dependency of its own Python build; depending on that is how a
# base image bump turns into a mystery failure.
RUN apt-get update \
 && apt-get install --no-install-recommends -y openssl \
 && rm -rf /var/lib/apt/lists/*

COPY tests/ ./tests/
COPY evals/ ./evals/
COPY scripts/ ./scripts/
COPY Makefile ./

# The suite includes repository-contract tests — the compose topology, the env
# template, the edge config. Shipping them here is what lets `docker compose run
# worker pytest` be the same suite as `make test`, rather than a subset of it.
COPY Dockerfile docker-compose.yml docker-compose.dev.yml Caddyfile .env.example ./

# pytest writes its cache next to the tree; without this the run is a wall of
# permission warnings.
RUN chown -R fittrack:fittrack /app

USER fittrack
CMD ["python", "-m", "fittrack.worker"]
