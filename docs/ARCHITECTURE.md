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

When a receiver answers with `Retry-After` (typically on a `429`, also valid on
`503`), Stinger honors it: the header value — delta-seconds or an HTTP-date — is
parsed and used as the next delay instead of the schedule slot, clamped to a
ceiling so a receiver can't push a retry arbitrarily far out, and jittered
*upward only* so we never come back **before** the time the receiver asked for.
Crucially, `Retry-After` moves *when* the next attempt happens, not *whether*:
the attempt still counts against the retry budget, so a receiver that keeps
asking for more time cannot hold a delivery open forever. This is the difference
between a backoff schedule and being a polite citizen of someone else's rate
limiter — we respect explicit backpressure rather than hammering our own
fixed cadence into a service that's already told us to slow down.

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

Because the listener is an optimization, losing it is survivable — but a platform
that silently degrades to poll-interval latency on the first database blip and
never recovers isn't production-grade. So the listener **self-heals**: it runs in
a reconnect loop with capped exponential backoff, and on every successful
(re)connect it wakes the worker once. That wake matters for correctness of the
*optimization* — `NOTIFY` is lossy, so any pings that fired while the listener
was disconnected were missed, and the wake forces a poll that sweeps up whatever
arrived during the gap. The connection-lost path is driven by asyncpg's
termination callback, so a dropped connection is detected promptly rather than
discovered only on the next failed use.

### Worker concurrency and shutdown

The set of in-flight tasks *is* the concurrency limiter: each round claims
`max_concurrency − len(in_flight)`, which keeps "how many I'm running" and "how
many I claim" tied to one number and honors the rule that you never lease more
than you can finish before the lease expires. A task finishing wakes the loop to
refill promptly. On `SIGTERM` the loop stops claiming and drains in-flight
deliveries; anything not finished is covered by lease expiry.

### Per-endpoint concurrency cap

A global concurrency bound alone isn't enough for fairness: one slow endpoint
whose deliveries each hang for the full timeout can occupy every slot in the
pool, starving every other endpoint behind it. So each endpoint also has its own
in-flight cap — `endpoints.max_concurrent_deliveries`, defaulting to a global
value (10) when `NULL` — and no endpoint may exceed it regardless of how much of
its backlog is due.

The cap is enforced **in the claim query, not in worker memory**, so it holds
across every worker process rather than per-process. The key is that it needs no
new bookkeeping: "in flight for this endpoint" is exactly `COUNT(*) WHERE
locked_by IS NOT NULL GROUP BY endpoint_id`, the same `locked_by IS NOT NULL ⇔
in flight` invariant the lease already maintains. The claim CTE counts current
leases per endpoint, ranks the due candidates per endpoint with a window
function, and admits a row only while `rank + current_in_flight ≤ cap`. (Window
functions can't coexist with `FOR UPDATE` at one query level in Postgres, so the
`SKIP LOCKED` candidate lock and the ranking live at separate CTE levels, and the
in-flight count is a separate aggregate read — locking the already-leased rows
would make `SKIP LOCKED` undercount them.)

Two consequences worth stating. First, head-of-line fairness: if the query only
locked `limit` candidates and a saturated endpoint's rows sorted to the front,
those rows would fill the candidate set and crowd out admissible work for other
endpoints. The claim over-fetches a larger candidate window than it intends to
claim so the cap filter has alternatives to admit — a mitigation, not a
guarantee against a pathological flood. Second, this gives **rough per-endpoint
FIFO ordering** as a side effect, since the per-endpoint rank is ordered by
`(next_attempt_at, id)`. A crashed worker's lease counts toward its endpoint's
in-flight total until the lease expires (~30s), conservatively under-filling that
endpoint for one lease window — it self-heals via the same expiry that recovers
the delivery itself.

### Circuit breaker

A permanently-dead endpoint shouldn't burn the full retry schedule on every
event published to it. Each endpoint carries a `consecutive_failures` counter,
incremented on every failed attempt and **reset to zero on any success**, all
inside `record_attempt`'s transaction so the count can never diverge from the
outcome it reflects. Past a threshold (default 20, configurable) the endpoint is
disabled. Reset-on-success is what makes this target the right endpoints: a
flaky endpoint with successes mixed in keeps resetting and never trips, so the
breaker fires only on *consistently* failing endpoints. *Tradeoff:* a
consecutive-failure count is coupled to traffic volume rather than wall-clock
time — a high-traffic endpoint trips in seconds, a low-traffic one in hours — so
the threshold is set high enough to ride out a transient blip. A sustained-time
window would decouple from volume at the cost of extra state.

The trip is a **one-time transition**: the disable `UPDATE` carries `WHERE
status = 'enabled'`, so concurrent failures crossing the threshold together
disable exactly once and stamp `disabled_at` exactly once.

A disabled endpoint's already-queued deliveries are handled by a **worker-side
gate**: they're still claimed, but the worker checks `endpoint.status` before
signing and, if disabled, marks the delivery `discarded` (no POST) instead of
attempting it — this is where the `discarded` status earns its keep. *Rejected:*
eagerly bulk-updating all the endpoint's pending rows at trip time races with
deliveries other workers are mid-POST on; parking them (leave pending, resume on
re-enable) either churns the claim with re-claim-and-skip or forces a join into
the hot claim query. The gate is race-free (each delivery is discarded by the
one worker holding its lease, through the same CAS), keeps the claim query
single-table, and drains the backlog with zero HTTP to the dead host. The only
cost is a tiny staleness window — an endpoint loaded just before it's disabled
gets one more POST, which is benign.

Recovery is **automatic, via a half-open probe**, with manual re-enable still
available as an override. A disabled endpoint sits for a cooldown; then a
periodic worker sweep transitions it `disabled → half_open` and enqueues a single
**trial delivery** — a re-drive of the endpoint's most recent delivery, so a
successful probe also delivers a real event that was previously dropped. If the
trial succeeds the endpoint goes `half_open → enabled` (counter reset,
`disabled_at` cleared); if it fails it drops straight back to `disabled` with a
*fresh* cooldown, so a still-dead endpoint is probed on a slow heartbeat rather
than hammered. The whole cycle resolves inside the existing `record_attempt`
transaction, so the endpoint's state never diverges from the trial's outcome.

Three properties make this safe with no new locking. Fan-out targets only
*enabled* endpoints, so a half-open endpoint receives no organic traffic — its
trial is the *only* delivery it has in flight, which is why the worker gate lets
a half-open attempt through where it discards a disabled one, and why the
cooldown is set to dwarf the lease (a trial always resolves long before the next
sweep could consider re-probing). The `disabled → half_open` transition is the
same one-time CAS as the trip (`WHERE status = 'disabled'`), so when many workers
sweep concurrently exactly one wins and enqueues exactly one trial. And even the
pathological interleaving is benign: whichever attempt records first wins the
`half_open` transition through its CAS, and a straggler finds the status already
moved and falls through harmlessly. Auto-disable plus half-open recovery — with
re-enable-and-replay still there for an operator who's fixed the receiver and
doesn't want to wait out the cooldown — is the loop that distinguishes a real
platform from `requests.post` in a loop. A `410 Gone` currently counts toward the
threshold like any other failure; it is arguably a stronger "disable me now"
signal, but treating it uniformly is the simpler choice for now.

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

### Response-body streaming cap

The response body is streamed off the wire with a hard byte ceiling rather than
buffered in full before truncation. Without a wire cap, a receiver that streams a
multi-megabyte body fast enough to outrun the 15-second deadline would consume
memory proportional to body size, not to time. The deadline and the cap are
complementary constraints: the deadline bounds *time*, the cap bounds *size*, and
together they close both attack angles.

The delivery also sends `accept-encoding: identity`, disabling compression. A
compressed response is decoded before the byte count is checked, so without this
header a receiver could serve a small compressed payload that expands to an
arbitrarily large decoded body — a classic zip-bomb variant. With `identity`,
what's on the wire is what's counted.

Two constants govern the behaviour: `MAX_RESPONSE_WIRE_BYTES` (the ceiling on
bytes pulled off the wire), and the existing `MAX_RESPONSE_BODY` (the cap on
characters retained in the attempt row). A small response is never truncated;
a large one stops being read at the wire ceiling, and whatever was read is then
trimmed to `MAX_RESPONSE_BODY` before storage. *Rejected:* raising the read
timeout instead — a longer timeout doesn't bound size at all and would delay
crash recovery for every timed-out delivery.

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

### Signing secrets at rest

Signing secrets cannot be hashed the way API keys are: HMAC needs the raw key
bytes at delivery time, so the secret must exist in recoverable form. The answer
is envelope encryption, not a single static key.

**Why envelope, not direct encryption:** each secret is sealed under its own
fresh 256-bit data key (DEK) using AES-256-GCM; the DEK is then *wrapped* under
a long-lived key-encryption key (KEK). The stored column token carries the
wrapped DEK alongside the ciphertext, so the column is fully self-contained. This
buys two things a direct-encryption approach can't: KEK rotation without
rewriting ciphertext (rewrap the small DEKs, leave the ciphertext alone), and a
clean KMS seam — the KEK provider is the only thing that ever touches the KEK, so
a future `KmsKeyProvider` (AWS KMS, Vault, HSM) is a drop-in that keeps the KEK
out of the process entirely.

**Token format** (stored verbatim in the existing `TEXT` column, no schema
change):

```
stcr.v1.<provider>.<wrapped_dek>.<dek_nonce>.<nonce>.<ct>   (each field base64url)
```

The `stcr.` prefix makes encrypted tokens trivially distinguishable from legacy
`whsec_…` values, which the data migration uses to seal idempotently. The
version and provider id fields are the GCM additional authenticated data for
both inner and outer AEAD layers, so tampering with the header is rejected at
open time, not silently ignored.

**The default `LocalKeyProvider`** derives its KEK from `STINGER_ENCRYPTION_KEY`
(or `SECRET_KEY` as a zero-config fallback) via HKDF-SHA256 with a fixed,
scheme-specific info string. The derived KEK is a distinct value from the
cookie-signing key even when the same source material is used. No additional
stateful dependency is introduced — Postgres remains the only one.

**The practical consequence:** a database dump no longer exposes signing secrets.
An attacker with a copy of Postgres cannot forge webhook deliveries without also
obtaining the KEK from the environment.

**The operational trade-off:** the encryption key is now load-bearing. Losing it
makes all signing secrets unrecoverable; every receiver would need to be
re-provisioned with a new secret. It must be backed up with the same care as a
private key.

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

## Management surface

Creating endpoints and event types is exposed three ways — the admin CLI, an
authenticated JSON API (`/api/v1/endpoints`, `/api/v1/event-types`), and
dashboard forms — but the creation rules live in **exactly one place**, a
`management` core that all three call. Event-type resolution, the http/https URL
validation, the optional per-endpoint concurrency cap (validated `≥ 1`,
mirroring the `max_concurrent_positive` CHECK), signing-secret generation and
sealing, and subscription wiring are written once; the surfaces only translate
transport and errors (a duplicate
becomes a `409` on the API, an inline form error on the dashboard, a non-zero
CLI exit). The alternative — re-deriving "create an endpoint" in each entry
point — is how three subtly different behaviours drift into existence; a created
endpoint should be identical no matter how it was created.

Two deliberate seams. The signing secret is returned exactly once at creation and
only its sealed form is ever persisted (the same show-once contract as API keys
and rotation), so no surface can leak it on a later read. And URL validation is
*format only* — scheme and host — not reachability: the worker's SSRF guard
resolves and pins the address at delivery time, which is the real security
boundary, so validating reachability at creation would only add flakiness (DNS,
transient outages) and false confidence that a host vetted once stays safe.

The CLI remains the bootstrap path for the very first application and key,
because those precede any credential that could authenticate an API call — the
same chicken-and-egg that keeps key minting out-of-band. Everything after that
first key can be driven from the API or the dashboard without touching the CLI.

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

### CSRF protection on dashboard forms

All dashboard `POST` routes carry a per-session CSRF token in addition to the
signed session cookie. `SameSite=lax` blocks cross-site form submissions in most
browser contexts, but `lax` applies only to top-level navigations and is not
sufficient for HTMX's `hx-post` button attributes, which do not qualify. A
dedicated token closes that gap.

The token is a `secrets.token_urlsafe(32)` value stored lazily in the session on
first access and injected into every template via a Jinja2 global. Validation
checks two paths to cover the two request shapes the dashboard uses:

- **`X-CSRF-Token` header** — `hx-headers` on `<body>` makes HTMX attach the
  token to every mutating request automatically, covering both form submissions
  and bare `hx-post` button actions (re-enable, rotate-secret).
- **`csrf_token` hidden input** — present in every `<form>` for the non-HTMX
  login form and as defense-in-depth on HTMX forms.

The `verify_csrf` FastAPI dependency checks the header first; if absent, it reads
the form body via Starlette's cached `request.form()`. `hmac.compare_digest`
(stdlib) guards the comparison — no new package dependency.

The token lives inside the existing signed session cookie (Starlette
`SessionMiddleware`), so there is no separate CSRF cookie and no new state. It is
cleared on logout with the rest of the session.

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

- **Time-window disable trigger.** The breaker counts *consecutive failures*,
  which couples the trip to traffic volume; a sustained-time-window trigger
  ("failing continuously for >1h") is more volume-robust but needs extra state.
- **Payload transformations, fan-in/aggregation, and per-endpoint rate limiting**
  are out of scope by design.

---

## Technology

Python 3.14 (standard-library `uuid7`; C-extension wheel availability verified
across the async stack). FastAPI, async SQLAlchemy 2.0 + asyncpg, Alembic,
httpx. PostgreSQL is the sole datastore. One image, two run modes (`api`,
`worker`). Tests run against real Postgres via testcontainers.