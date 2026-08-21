FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependências primeiro: a camada só invalida quando o pyproject muda.
COPY pyproject.toml ./
RUN pip install --no-cache-dir hatchling && \
    pip install --no-cache-dir -e ".[dev]" 2>/dev/null || pip install --no-cache-dir -e .

COPY src/ ./src/
COPY alembic.ini* ./

# Nunca root: um exploit no processo não vira root no container.
RUN useradd --create-home --uid 10001 fittrack && chown -R fittrack:fittrack /app
USER fittrack

EXPOSE 8000
CMD ["uvicorn", "fittrack.main:app", "--host", "0.0.0.0", "--port", "8000"]
