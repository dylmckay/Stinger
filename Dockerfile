# Stage 1: Builder (Uses full Bookworm to compile uvloop/asyncpg)
FROM python:3.14-bookworm as builder

WORKDIR /app

# Prevent python from writing bytecode 
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1


# Install build tools and dependencies
COPY pyproject.toml uv.lock ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential python3-dev \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /install \
    && /install/bin/python -m pip install --no-cache-dir uv \
    && /install/bin/python -m uv sync --locked --active --no-install-project --no-install-workspace --no-install-local --no-cache

# Stage 2: Runtime (Uses slim to keep image small)
FROM python:3.14-slim-bookworm

WORKDIR /app

# Copy the compiled libraries from the builder stage
COPY --from=builder /install /install

# Copy application code
COPY . .

EXPOSE 8000

CMD ["/install/bin/python", "-m", "fastapi", "dev", "app/main.py", "--host", "0.0.0.0", "--port", "8000"]
