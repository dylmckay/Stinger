# Stinger — Architecture

Stinger is a self-hostable webhook delivery platform: you publish events to it,
and it reliably delivers signed HTTP callbacks to your customers' endpoints,
with retries, an auditable attempt history, and a dashboard to answer the only
question that matters when a webhook goes missing — *did it fire, and what
happened?*

This document records the design decisions behind it and, more importantly,
*why* each was made and what was rejected. The guiding constraint shapes almost
everything below:

> **Postgres is the only stateful dependency.** A self-hoster runs `docker
> compose up` and gets a working platform backed by one database. Every
> component we *don't* require is a feature.

## System overview

A single application image runs in two modes selected at startup:

- **api** — the ingest endpoint and the dashboard read API (FastAPI).
- **worker** — the long-running delivery loop.

The end-to-end path of an event:

```
publish (authenticated)
  → persist event + fan out to one delivery row per subscribed endpoint   [one txn]
  → NOTIFY  ──────────────────────────────────────────────────────────────┐
                                                                           ▼
  worker:  claim due deliveries (SKIP LOCKED + lease)                  (wakes early)
        → sign (HMAC-SHA256)
        → POST (SSRF-guarded, timeout-bounded)
        → record outcome (succeed / retry-with-backoff / exhaust)
```

Everything that follows explains the choices inside that pipeline.

---

## Data model

### Events, deliveries, and attempts are three separate entities

A publisher sends **one event**; that event fans out to **N deliveries** (one
per subscribed endpoint), and each delivery accumulates immutable **attempt**
records. This separation is the backbone of the whole system. Deliveries are
mutable state machines with their own retry state; events and attempts are
append-only. It makes replay trivial (insert a fresh delivery for an existing
event) and makes "what happened to this webhook?" a single query against the
attempt timeline.

### UUIDv7 primary keys

Ids are public — they travel in delivery headers, consumers dedupe on them, they
appear in support tickets — so sequential integers are out (they leak volume).
Plain UUIDv4 works but scatters inserts randomly across the B-tree. **UUIDv7** is
time-ordered: inserts append rather than scatter (better index locality), ids
sort chronologically for free, and keyset pagination becomes trivial because
Postgres compares `uuid` byte-wise and the v7 timestamp is in the most
significant bytes. Generated with the Python 3.14 standard-library `uuid.uuid7()`,
so no extra dependency.

### Payloads stored as `TEXT`, canonicalized once at ingest

The signature must cover the *exact bytes* we deliver, so the stored,
delivered, and signed representations must be identical. We store the payload as
`TEXT` (not `JSONB`) because `JSONB` stores a *parsed* form and re-serializing it
can reorder keys and strip whitespace — meaning the delivered body could differ
from what was signed. The payload is normalized exactly once, at the ingest
boundary (`json.dumps(..., separators=(",",":"))`, key order preserved), and
never re-serialized again. *Tradeoff:* this is not byte-identical to the
publisher's original whitespace; the property we actually need — stored == sent
== signed, with no re-serialization after storage — holds completely. If a JSONB
query surface is ever needed, it can be added as a derived column without
touching the canonical text.

### Status columns are `TEXT` + `CHECK`, not native enums

Native Postgres enums look clean until you add a value: `ALTER TYPE ... ADD
VALUE` has awkward transactional restrictions and Alembic won't autogenerate the
change. A `TEXT` column with a `CHECK (status IN (...))` constraint gives the
same integrity, and evolving the set is one constraint swap. A Python `StrEnum`
keeps it typed in application code.

### Subscriptions are a join table, not an array column

Endpoint→event-type subscriptions live in an `endpoint_event_types` join table
rather than a `TEXT[]` on endpoints. The join table gives real foreign-key
integrity against the event-type registry (you can't subscribe to a typo) and
makes the fan-out query — *which endpoints get this event?* — a plain indexed
join, which is one of the hottest reads in the system.

### Conventions

`TIMESTAMPTZ` everywhere, UTC, server-defaulted. A constraint **naming
convention** is set on the SQLAlchemy `MetaData` *before* the first migration, so
constraints have deterministic names that later migrations can reference. There
is deliberately **no unique constraint on `(event_id, endpoint_id)`** in
`deliveries` — replay works by inserting additional delivery rows for the same
pair, so uniqueness there would break the feature.

---

## The delivery queue

### Postgres is the queue (no Celery, no Redis)

Webhook delivery is dominated by *scheduled retries* ("try again in 10 minutes").
A Celery + Redis broker handles this poorly: ETA/countdown tasks are prefetched
into worker memory, long-delayed tasks interact badly with the broker's
visibility timeout, and the scheduled work becomes invisible — you can't ask
"what's pending for endpoint X?" because it lives in the broker, not your
database.

So the `deliveries` table *is* the queue. Workers claim work with `SELECT ...
FOR UPDATE SKIP LOCKED`, which lets many workers poll concurrently without
contending or double-claiming. This buys transactional fan-out for free (the
event insert and its delivery rows commit atomically — no "event saved but
deliveries lost" window), a fully SQL-inspectable queue, and a stateful
footprint of exactly one service. *Rejected:* Redis Streams with consumer groups
is more "event-native," but scheduled retries still force a separate delayed-set,
and you lose atomicity with the source of truth.

### Crash recovery via a visibility-timeout lease, not a long transaction

A row lock from `FOR UPDATE` lives only as long as its transaction. Holding that
transaction open across the HTTP call (so a crash auto-releases the row) is
simple but pins one DB connection per in-flight delivery and is a long-running
transaction anti-pattern — it would cap concurrency at pool size.

Instead we **claim and release**: in a short transaction, lock due rows with
`SKIP LOCKED`, then push their `next_attempt_at` into the future (a ~30s lease)
and stamp `locked_by`, and commit immediately. The HTTP call holds no
transaction. This means a small connection pool drives a large number of
concurrent deliveries, because connections are held for milliseconds, not for the
duration of someone else's slow endpoint.

The elegance is that **the lease reuses `next_attempt_at`**: a claimed row is
simply invisible to other pollers until the lease expires, and a crashed worker's
row becomes claimable again the instant its lease passes `now()` — recovered by
the *ordinary* claim query, with no separate reaper process and no extra index.

### The record step: state machine, CAS guard, and attempt counting

Recording an outcome runs in one short transaction that advances the delivery
(`succeeded` / `retrying` with the next backoff / `exhausted`), appends an
immutable attempt row, and releases the lease (clears `locked_by`, restoring the
invariant *`locked_by IS NOT NULL` ⇔ in flight*).

Two correctness details:

- **`attempt_count` is incremented at record time, not claim time.** A crash
  between claim and delivery therefore never burns a retry that didn't happen.
- **A compare-and-swap guards the finalize:** `UPDATE ... WHERE id = :id AND
  locked_by = :worker_id`. If this worker overran its lease and another already
  re-claimed the row, the update matches zero rows and the result is *discarded*
  rather than clobbering the new owner. The duplicate POST that resulted is fine —
  the system is at-least-once and consumers dedupe.

### Retry schedule

A fixed schedule with bounded jitter, encoded as data (it's a documented
contract):

```
5s → 30s → 2m → 10m → 1h → 4h → 12h   then exhausted   (8 attempts total)
```

Each delay carries ±20% jitter so a batch of deliveries that fail simultaneously
(e.g. an endpoint goes down) don't all retry at the exact same instant and
stampede it on recovery. *Rejected:* full jitter (`random(0, delay)`) has
slightly better contention behavior but can retry almost immediately, surprising
operators who read "retries after ~5 minutes"; bounded jitter trades a little
smoothing for legibility. All scheduling resolves against Postgres's clock
(`now()`), never the worker's wall clock, so lease and backoff timing never
depend on whose clock you trust.

### Polling is the floor; `LISTEN/NOTIFY` is an optimization

The worker polls on an interval, and that poll is the *only* thing correctness
depends on — a plain claim always finds both due rows and reclaimable expired
leases. On top of that, ingest fires an empty-payload `NOTIFY` on commit, and a
dedicated listener connection wakes the worker so fresh events deliver in
milliseconds instead of waiting out the poll interval. `NOTIFY` is treated as a
*hint*, never the queue: it's lossy (delivered only to whoever is listening at
that instant) and it can't schedule a future retry, so the system must be — and
is — fully correct with polling alone. The listener uses a raw connection held
*outside* the SQLAlchemy pool, because a pooled connection would eventually be
recycled and silently drop the `LISTEN`.

### Worker concurrency and shutdown

The set of in-flight tasks *is* the concurrency limiter: each round claims
`max_concurrency − len(in_flight)`, which keeps "how many I'm running" and "how
many I claim" tied to one number and honors the rule that you never lease more
than you can finish before the lease expires. A task finishing wakes the loop to
refill promptly. On `SIGTERM` the loop stops claiming and drains in-flight
deliveries; anything not finished is covered by lease expiry.

---

## HTTP delivery

### Timeout discipline and no redirects

A shared `httpx.AsyncClient` (connection pooling), componentized timeouts
(connect 3s, read/write 10s) and a hard outer `asyncio.timeout(15s)` ceiling so a
slow-dripping response can't outlive the lease. **Redirects are disabled** — a
webhook receiver has no business redirecting us, and following a 3xx is a classic
SSRF bypass. A redirect becomes a plain non-2xx failure.

### SSRF guard with IP pinning

Endpoint URLs are attacker-controlled, so the worker is a confused-deputy risk: a
malicious tenant could point an endpoint at `http://169.254.169.254/` (cloud
metadata) or an internal service. Before every POST the guard resolves the host,
validates **every** resolved address against blocked ranges — loopback, RFC-1918
private, link-local (which covers the metadata address), multicast, reserved,
unspecified, and IPv4-mapped IPv6 (so `::ffff:127.0.0.1` can't sneak through) —
and then **pins** the connection to the validated IP rather than letting the
client re-resolve. Pinning closes the DNS-rebinding TOCTOU window: an attacker's
DNS can't return a safe IP for the check and a malicious one for the connect. The
hostname is preserved for the `Host` header and TLS SNI. The blocklist is
bypassable via an `allow_private` flag, because "internal" is network-specific —
self-hosters and the test suite need to opt into local targets.

### Outcome classification

The HTTP layer classifies and the record layer just records, communicating
through an `AttemptResult`. `2xx` is success. `410 Gone` is the one non-retryable
failure (the consumer is saying *stop*), so the delivery exhausts immediately.
Everything else — other 4xx/5xx, timeouts, connection errors, SSRF blocks — is a
failure; transient ones retry, the SSRF block does not (a misconfigured internal
URL won't fix itself).

---

## Signing

### HMAC-SHA256, Standard Webhooks compatible

Each delivery carries `webhook-id`, `webhook-timestamp`, and `webhook-signature`
headers. The signature is HMAC-SHA256 over `{id}.{timestamp}.{payload}`, base64,
version-prefixed (`v1,<sig>`). Each component is load-bearing: the id binds the
signature to *this* message (a captured signature can't be replayed against a
different payload), the timestamp lets the consumer reject stale replays, and the
payload is the exact stored bytes. The header carries a *space-separated list* of
tokens so that during secret rotation we sign with both the active and previous
secret and the consumer accepts if any verifies.

This deliberately matches the [Standard Webhooks](https://www.standardwebhooks.com)
spec — verified byte-for-byte against the reference library — so consumers can
verify Stinger's deliveries with off-the-shelf libraries in any language instead
of hand-rolling it. The `v1` prefix is the algorithm-agility hook for future
schemes.

Consumer-side verification uses a constant-time comparison (`hmac.compare_digest`)
and a timestamp tolerance window for replay protection. Constant-time matters
here specifically because the consumer compares a secret-derived value an
attacker partially controls.

---

## Ingest

Publishing is one transaction: resolve the event type, insert the event, fan out
to every *enabled* subscribed endpoint, fire `NOTIFY`, commit. The single
transaction is the entire reason for Postgres-as-queue — there is never a window
where the event exists but its deliveries don't. Zero subscribers is valid: the
event is stored, no deliveries, no notify.

**Idempotency** is via `INSERT ... ON CONFLICT DO NOTHING` on `(application_id,
idempotency_key)`: a duplicate returns the existing event and does *not*
re-fan-out, making publish safely retryable. Null keys never conflict, so keyless
publishes always create fresh events.

---

## Authentication

### SHA-256 for API keys, not bcrypt

This is the opposite of password hashing, on purpose. Bcrypt/argon2 are
deliberately slow to defend *low-entropy* human secrets; an API key we generate
is a 192-bit random value, so brute force is infeasible regardless of hash speed
and the slowness buys nothing. Worse, bcrypt's per-key salt means you can't look
a key up by its hash — you'd fetch candidates and bcrypt-compare each, O(n) slow
hashes per request. A plain **SHA-256** of a high-entropy key is deterministic,
so authentication is one indexed equality lookup; no salt is needed because
salting defends against rainbow tables for reused/low-entropy secrets, and a
random 192-bit key is in nobody's table.

Notably there is **no app-side constant-time compare** in key auth (unlike
signature verification): forging a matching hash would require a SHA-256
*preimage*, so an indexed equality lookup leaks nothing exploitable.

### Key handling and bootstrap

Keys are `sk_`-prefixed, shown to the creator exactly once, and stored only as
their hash plus a non-secret display prefix — the full key is unrecoverable
afterward. Authentication is `Authorization: Bearer`, checks revocation, and
updates `last_used_at` on a throttle (only when stale by >60s) to avoid a write
per request. There is deliberately **no public key-creation endpoint** — that
would need a key to authenticate, a chicken-and-egg — so the first key is minted
out-of-band via an admin CLI; user-facing key management comes later via the
authenticated dashboard.

---

## Read side

### Keyset pagination, not OFFSET

Lists that grow without bound (events, deliveries) use cursor/keyset pagination
on the UUIDv7 id: `WHERE id < :cursor ORDER BY id DESC LIMIT n`. `OFFSET N` scans
and discards N rows (deep pages get linearly slower) and shifts under concurrent
inserts so a row can appear twice or be skipped. Keyset is O(log n) per page and
stable, and the cursor is simply the last *returned* row's id — a subtle but
critical point, since using the peeked "is there more?" row as the cursor silently
drops a row at every page boundary.

### Tenant isolation by construction

Deliveries and attempts have no `application_id` of their own; every read reaches
them only *through* their event's `application_id`. A detail query that doesn't
match the caller's application returns nothing — there is no code path by which
one tenant reads another's data.

---

## Delivery semantics (the contract)

Stated explicitly, because these are what make the project infrastructure rather
than a demo:

- **At-least-once.** A crash after the POST but before the DB write yields a
  redelivery, so consumers **must dedupe on `webhook-id`**.
- **Idempotent publish.** Provide an idempotency key and publishing is safely
  retryable.
- **Best-effort ordering only.** Strict ordering would force serialization that
  kills throughput; we don't promise it.

---

## Deferred / known limitations

Honest scope boundaries, listed so their absence reads as a decision:

- **Response-body memory cap.** The current attempt reads the full response body
  before truncating; a hardened version streams with a byte cap. The 15s deadline
  bounds a slow drip but not a fast flood.
- **Endpoint circuit breaker.** A permanently-dead endpoint currently burns the
  full retry schedule on every event. Auto-disable after sustained failure (the
  `consecutive_failures` / `disabled_at` columns already exist) and worker-side
  honoring of `endpoint.status` are the next increment.
- **Per-endpoint concurrency cap.** Global concurrency is bounded; a per-endpoint
  cap (to stop one slow consumer monopolizing the pool and to give rough
  per-endpoint ordering) is a refinement.
- **`Retry-After` honoring** on 429 is not yet wired into the backoff.
- **Payload transformations, fan-in/aggregation, and per-endpoint rate limiting**
  are out of scope by design.

---

## Technology

Python 3.14 (standard-library `uuid7`; C-extension wheel availability verified
across the async stack). FastAPI, async SQLAlchemy 2.0 + asyncpg, Alembic,
httpx. PostgreSQL is the sole datastore. One image, two run modes (`api`,
`worker`). Tests run against real Postgres via testcontainers.