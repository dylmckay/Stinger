# syntax=docker/dockerfile:1
#
# One image, three run modes (api / worker / migrate) — selected by the
# container command in docker-compose.yml, not by separate images.

# ---- builder: resolve locked deps into a venv with uv ----
FROM python:3.14-slim AS builder

# uv for fast, reproducible installs. Pin uv itself for repeatable builds.
RUN pip install --no-cache-dir uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

# Install dependencies first (their own cache layer), without the project,
# so app code changes don't invalidate the dependency layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Then the project itself.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime: slim, non-root, just the venv + app ----
FROM python:3.14-slim AS runtime

RUN groupadd --system stinger && useradd --system --gid stinger stinger
WORKDIR /app

COPY --from=builder --chown=stinger:stinger /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER stinger
EXPOSE 8000

# Default mode is the API; compose overrides command for worker and migrate.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
