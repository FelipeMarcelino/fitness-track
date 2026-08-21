#!/usr/bin/env bash
# Runs exactly what CI runs. Verifying a narrower scope than CI is how
# "passes locally, fails in CI" happens -- it already did twice on this sprint.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
echo "--- ruff check ---";  $PY -m ruff check .
echo "--- ruff format ---"; $PY -m ruff format --check .
echo "--- mypy ---";        PYTHONPATH=src $PY -m mypy src
echo "--- pytest ---";      PYTHONPATH=src $PY -m pytest -q
echo "--- compose ---";     docker compose config --quiet && echo "compose ok"
echo "ALL GREEN"
