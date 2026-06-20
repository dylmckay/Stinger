# Stinger 🐝

A self-hostable webhook delivery platform. Publish events to it, and it
reliably delivers signed HTTP callbacks to your customers' endpoints — with
retries and backoff, HMAC signatures, a circuit breaker for dead endpoints, and
a dashboard that answers the only question that matters when a webhook goes
missing: *did it fire, and what happened?*

Stinger's one design constraint shapes everything: **Postgres is the only
stateful dependency.** No Redis, no message broker. You run `docker compose up`
and get a working platform backed by a single database.

> **Status: v0.2.0.** The delivery engine, signing, SSRF protection, circuit
> breaker (with half-open auto-recovery), secret rotation, encryption of signing
> secrets at rest, a management API + dashboard forms, CSRF protection on
> dashboard forms, and `Retry-After` honoring are built and tested against real
> Postgres. See [Project status](#project-status) for what's deferred.

---

## Features

- **At-least-once delivery** with a fixed retry schedule and bounded jitter
  (`5s → 30s → 2m → 10m → 1h → 4h → 12h`, then exhausted), and **`Retry-After`
  honoring** so a `429`/`503` with a backoff hint is respected instead of
  overridden.
- **HMAC-SHA256 signatures**, [Standard Webhooks](https://www.standardwebhooks.com)
  compatible — your consumers verify with off-the-shelf libraries in any language.
- **Secret rotation** with a dual-sign overlap window, so you rotate signing
  secrets without a verification gap.
- **Signing secrets encrypted at rest** with envelope encryption (per-secret
  data key, wrapped by a key derived from your environment) — a database dump no
  longer exposes the secrets needed to forge deliveries.
- **Circuit breaker with auto-recovery** — endpoints that fail consistently are
  auto-disabled, then automatically probed with a single trial delivery after a
  cooldown and re-enabled on success; manual re-enable + replay is still there.
- **Per-endpoint concurrency cap** — a slow consumer can't monopolize the worker
  pool; each endpoint has an in-flight cap (default global, overridable per
  endpoint), enforced in the claim query so it holds across all workers.
- **SSRF protection** — every delivery target is resolved and validated against
  private / loopback / metadata IP ranges, with the connection pinned to the
  vetted IP to close DNS-rebinding races.
- **Idempotent publish** — retry a publish safely with an idempotency key.
- **Manage everything without the CLI** — create and list endpoints and event
  types over an authenticated JSON API or the dashboard; the CLI only bootstraps
  the first application and key.
- **Replay** — re-drive any delivery from the dashboard or API.
- **Observable** — a server-rendered dashboard with a per-delivery attempt
  timeline, endpoint health, and an event log.
- **Postgres-as-queue** — transactional fan-out via `FOR UPDATE SKIP LOCKED`;
  the whole queue is inspectable with plain SQL.

## How it works

A single image runs in two modes — `api` (ingest + dashboard) and `worker`
(delivery loop) — over one Postgres database:

```
publish (authenticated)
  → persist event + fan out to one delivery per subscribed endpoint   [one txn]
  → NOTIFY ───────────────────────────────────────────────────────────┐
                                                                       ▼
  worker:  claim due deliveries (SKIP LOCKED + visibility lease)   (wakes early)
        → sign (HMAC-SHA256)
        → POST (SSRF-guarded, timeout-bounded)
        → record outcome (succeed / retry-with-backoff / exhaust)
```

The full reasoning behind each decision — why Postgres-as-queue, why a lease
instead of a long transaction, why SHA-256 for keys but HMAC for payloads — is
in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Quickstart

Requires Docker and Docker Compose.

```bash
git clone https://github.com/dylmckay/stinger.git
cd stinger
cp .env.example .env          # a SECRET_KEY is pre-generated; regenerate for production
docker compose up -d --build  # starts postgres, runs migrations, then api + worker
```

The API is now on `http://localhost:8000` and the worker is delivering. Next,
bootstrap your first application, endpoint, and key with the admin CLI:

```bash
# 1. Create an application (a tenant) — prints its id
docker compose exec api python -m app.cli create-application "Acme"

# 2. Register an event type for it
docker compose exec api python -m app.cli add-event-type <application_id> invoice.paid

# 3. Add a receiving endpoint, subscribed to that event type
#    Prints the endpoint id and its signing secret (whsec_…) — give the secret
#    to whoever runs the receiver.
docker compose exec api python -m app.cli add-endpoint <application_id> \
    https://httpbin.org/post --event-type invoice.paid

# 4. Issue an API key (shown once)
docker compose exec api python -m app.cli issue-key <application_id> --name prod
```

The CLI bootstraps the first application and key (steps 1 and 4) because those
precede any credential that could authenticate an API call. Once you have a key,
steps 2 and 3 — registering event types and adding endpoints — can be done from
the dashboard or the JSON API instead; see
[Managing endpoints and event types](#managing-endpoints-and-event-types).

Publish an event with the key:

```bash
curl -X POST http://localhost:8000/api/v1/events \
  -H "Authorization: Bearer sk_…" \
  -H "Content-Type: application/json" \
  -d '{"event_type": "invoice.paid", "payload": {"amount": 1000}, "idempotency_key": "inv_123"}'
```

Then open `http://localhost:8000`, paste the same API key to sign in, and watch
the delivery land — including its full attempt timeline.

## Publishing events

`POST /api/v1/events` with a `Bearer` API key:

| Field             | Type   | Notes                                              |
| ----------------- | ------ | -------------------------------------------------- |
| `event_type`      | string | Must be a registered event type for the app.       |
| `payload`         | object | Delivered verbatim; the signature covers it.        |
| `idempotency_key` | string | Optional. A repeat publish returns the same event.  |

The event fans out to every enabled endpoint subscribed to that type. A repeat
publish with the same `idempotency_key` returns the existing event and does not
re-fan-out, so publishing is safe to retry.

## Managing endpoints and event types

Beyond the bootstrap CLI, endpoints and event types can be created and listed
over an authenticated JSON API or from the dashboard — a new user never has to
touch the CLI after minting a key.

JSON API (same `Bearer` key as publishing):

```bash
# Register an event type
curl -X POST http://localhost:8000/api/v1/event-types \
  -H "Authorization: Bearer sk_…" -H "Content-Type: application/json" \
  -d '{"name": "invoice.paid"}'

# Add an endpoint subscribed to one or more event types.
# The response includes the signing secret ONCE — store it now.
# max_concurrent_deliveries is optional — omit it to use the global default.
curl -X POST http://localhost:8000/api/v1/endpoints \
  -H "Authorization: Bearer sk_…" -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/webhooks", "event_types": ["invoice.paid"], "max_concurrent_deliveries": 5}'

# List them
curl http://localhost:8000/api/v1/event-types -H "Authorization: Bearer sk_…"
curl http://localhost:8000/api/v1/endpoints   -H "Authorization: Bearer sk_…"
```

The bootstrap CLI takes the same cap via `--max-concurrent` on `add-endpoint`.

On the dashboard, the **Endpoints** page has a create form (with a quick-add for
event types and an optional max-concurrent field), and the **Event types** page
manages the full set. Created endpoints show their signing secret once, in the
same reveal used by rotation.

## Receiving & verifying

Each delivery carries `webhook-id`, `webhook-timestamp`, and `webhook-signature`
headers. Verify them with any [Standard Webhooks](https://www.standardwebhooks.com)
library, or by hand — see [`docs/receiving-webhooks.md`](docs/receiving-webhooks.md)
for verification snippets and the rotation-window handling.

## Configuration

Set in `.env` (read by Docker Compose). See [`.env.example`](.env.example).

| Variable                | Required | Default | Purpose                                                              |
| ----------------------- | -------- | ------- | -------------------------------------------------------------------- |
| `SECRET_KEY`            | yes      | —       | Signs dashboard session cookies. Generate a fresh one for production. |
| `STINGER_ENCRYPTION_KEY` | no      | derived | Key material for encrypting signing secrets at rest. Falls back to a key derived from `SECRET_KEY`; set a dedicated value to rotate it independently. Back it up like a private key — losing it makes stored signing secrets unrecoverable. |
| `POSTGRES_USER/PASSWORD/DB` | yes  | —       | Credentials for the bundled Postgres service.                        |
| `DATABASE_URL`          | no       | derived | Composed from `POSTGRES_*`; set only to use an external database.     |
| `ALLOW_PRIVATE_TARGETS` | no       | `false` | Allow delivery to private/loopback IPs. Keep `false` in production.   |

Generate a `SECRET_KEY`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Development

Stinger uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync                       # install deps (including the dev group)

uv run pytest                 # run the test suite (needs Docker for testcontainers)

# Run the services locally against your own Postgres:
uv run alembic upgrade head
uv run uvicorn app.main:app --reload    # api + dashboard on :8000
uv run python -m app.worker_main        # delivery worker
```

## Project status

v0.2.0. Built and tested: the delivery engine (fan-out, lease-based claim,
retries, crash recovery), a per-endpoint concurrency cap, HMAC signing with
rotation, signing secrets encrypted at rest, SSRF protection, the circuit breaker
with half-open auto-recovery, `Retry-After` honoring, a self-healing
`LISTEN/NOTIFY` listener, idempotent publish, replay, a management API and
dashboard forms for endpoints and event types, CSRF token protection on all
dashboard forms, and the dashboard with its per-delivery attempt timeline.

Known limitations and deferred work are tracked honestly in the
[architecture doc](docs/ARCHITECTURE.md#deferred--known-limitations) — including
a sustained-time-window breaker trigger and in-place editing of an endpoint's cap.

## License

MIT — see [`LICENSE`](LICENSE).