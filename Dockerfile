# syntax=docker/dockerfile:1
# Single image, two run modes (api / worker). Built with uv, runs non-root.

FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

# 1) Dependencies first — cached unless the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# 2) Then the application itself.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.14-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1000 stinger
WORKDIR /app
COPY --from=builder --chown=stinger:stinger /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER stinger
EXPOSE 8000

# Default mode; docker-compose overrides per service (api / worker / migrate).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
