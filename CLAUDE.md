# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                      # install deps including dev group
uv run pytest                                # run full test suite (requires Docker for testcontainers)
uv run pytest tests/test_ingest.py          # run a single test file
uv run pytest -k "test_name"                # run a single test by name
uv run alembic upgrade head                  # apply migrations
uv run alembic revision --autogenerate -m "description"  # generate a migration
uv run uvicorn app.main:app --reload         # run the API server (port 8000)
uv run python -m app.worker_main             # run the delivery worker
```

Docker-based full stack:
```bash
docker compose up -d --build                # start postgres + api + worker
docker compose exec api python -m app.cli create-application "Name"
docker compose exec api python -m app.cli issue-key <app_id> --name prod
```

## Architecture

**Two processes, one image.** `app.main:app` is the FastAPI API + dashboard server. `app.worker_main` is the delivery loop. Both connect to the same Postgres database. The startup mode is selected by which entrypoint you invoke, not an env flag.

**Postgres is the only stateful dependency.** No Redis, no Celery, no broker. The `deliveries` table is the queue, polled with `SELECT ... FOR UPDATE SKIP LOCKED`. `LISTEN/NOTIFY` is a latency optimization on top, not a correctness dependency.

### Module map

```
app/
  models.py          — SQLAlchemy ORM (all tables); statuses are StrEnum + TEXT+CHECK columns
  config.py          — pydantic-settings; Settings read from env
  ingest.py          — publish_event(): one transaction: event + fan-out deliveries + NOTIFY
  management.py      — core creation logic for endpoints and event types (single source of truth)
  auth.py            — API key hashing (SHA-256) and lookup
  crypto.py          — envelope encryption for signing secrets (LocalKeyProvider, HKDF→AES-GCM)
  reads.py           — dashboard read queries (keyset pagination on UUIDv7 ids)
  cli.py             — admin bootstrap CLI (first application + key only)
  main.py            — FastAPI app factory; mounts api/* routes then the web app
  worker_main.py     — asyncio entry point; starts Worker + LISTEN/NOTIFY listener task

  delivery/
    worker.py        — Worker: poll loop, concurrency gate, SIGTERM drain, half-open sweeper
    claim.py         — claim_deliveries(): SKIP LOCKED + lease push
    record.py        — record_attempt(): CAS update, backoff scheduling, circuit-breaker logic
    http.py          — make_delivery(): httpx call with timeout, redirect block, body cap
    signing.py       — HMAC-SHA256 per Standard Webhooks spec; dual-sign during rotation
    ssrf.py          — resolve + validate IP against blocked ranges, pin connection to vetted IP

  api/
    events.py        — POST /api/v1/events (publish)
    management.py    — POST/GET /api/v1/endpoints, /api/v1/event-types
    dashboard.py     — read-only JSON API consumed by dashboard HTMX fragments
    deps.py          — FastAPI dependency stubs (overridden in app factory and tests)

  web/
    app.py           — Starlette sub-app: session middleware, static files, template routes
    views.py         — Jinja2-rendered dashboard pages
    auth.py          — session-cookie login/logout
    deps.py          — web layer deps (session-authenticated user)

  static/
    templates/       — Jinja2 HTML templates (HTMX for partial updates)
```

### Key design decisions (read before changing core behaviour)

- **Lease-based claiming**: `claim_deliveries` pushes `next_attempt_at` ~30s into the future and stamps `locked_by`. The HTTP call holds no DB transaction. `record_attempt` uses a CAS (`WHERE id=:id AND locked_by=:worker_id`) to guard the finalize — a stale writer safely loses.
- **`attempt_count` increments at record time**, never claim time, so a crash never burns a retry.
- **Payload stored as TEXT**, normalized once at ingest with `json.dumps(..., separators=(",",":"))`. Never re-serialized. This keeps stored == signed == delivered bytes identical.
- **Circuit breaker**: `consecutive_failures` on `Endpoint`, reset to 0 on any success, tripped past threshold (default 20). Disabled endpoints' pending deliveries are `discarded` (no HTTP) by the worker-side gate, not bulk-updated at trip time.
- **Half-open auto-recovery**: worker sweeper transitions `disabled → half_open` after cooldown, enqueues one trial delivery (re-drive of most recent delivery). Success → `enabled`; failure → back to `disabled` with fresh cooldown.
- **Management core in `app/management.py`**: all three entry points (CLI, JSON API, dashboard) call this single module. Creation rules live in exactly one place.
- **Signing secrets** use envelope encryption (`crypto.py`): per-secret DEK (AES-256-GCM), wrapped by a KEK derived from `STINGER_ENCRYPTION_KEY` via HKDF-SHA256. Token format: `stcr.v1.<provider>.<wrapped_dek>.<dek_nonce>.<nonce>.<ct>`.
- **UUIDv7 primary keys** (`uuid.uuid7()` from Python 3.14 stdlib) — time-ordered for index locality and keyset pagination.
- **Tenant isolation**: deliveries/attempts have no `application_id`; reads always join through `events.application_id`.

### Testing

Tests use `testcontainers` to spin up a real Postgres 16 container (requires Docker). The `conftest.py` `engine` fixture creates and drops all tables around each test. `pytest-asyncio` is configured with `asyncio_mode = "auto"`. Set `SECRET_KEY` env var (any value) when running locally without `.env`.
