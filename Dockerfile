# syntax=docker/dockerfile:1
# Agent Relay API image.  Needs a PostgreSQL database: run it with
# `docker compose up -d --build` (see compose.yaml), or pass
# RELAY_DATABASE_URL=postgresql+psycopg://user:password@host:5432/dbname.

FROM python:3.11-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Install locked dependencies first so code edits don't invalidate this layer.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY *.py dashboard.html ./


FROM python:3.11-slim

RUN groupadd --system relay && useradd --system --gid relay --home-dir /app relay

WORKDIR /app
COPY --from=build --chown=relay:relay /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER relay
EXPOSE 8000

# /ready checks the real tables, so an unreachable or empty database reports unhealthy.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2).status == 200 else 1)"]

# Bind to all interfaces so the published port reaches the server.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
