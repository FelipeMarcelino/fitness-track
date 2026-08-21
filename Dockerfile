FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first: this layer is only invalidated when pyproject changes.
COPY pyproject.toml ./
RUN pip install --no-cache-dir hatchling && \
    pip install --no-cache-dir -e .

COPY src/ ./src/

# Never root: an exploit in the process does not become root in the container.
RUN useradd --create-home --uid 10001 fittrack && chown -R fittrack:fittrack /app
USER fittrack

EXPOSE 8000
CMD ["uvicorn", "fittrack.main:app", "--host", "0.0.0.0", "--port", "8000"]
