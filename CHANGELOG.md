# Changelog

All notable changes to Stinger are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_Nothing yet._

## [0.2.0] — 2026-06-17

Hardening, operability, and a management surface that removes the CLI from the
day-to-day path.

### Added

- **Signing secrets are encrypted at rest** using envelope encryption: each
  secret is sealed under its own AES-256-GCM data key, which is wrapped by a
  key-encryption key derived from `STINGER_ENCRYPTION_KEY` (falling back to a
  key derived from `SECRET_KEY`). The token is self-contained in the existing
  column, and the provider abstraction leaves a clean seam for a future KMS.
- **Response-body streaming cap.** Response bodies are streamed off the wire to a
  hard byte ceiling instead of being buffered in full, and delivery now sends
  `accept-encoding: identity` so a compressed body can't expand past the cap.
- **Management JSON API** for endpoints and event types: `POST`/`GET`
  `/api/v1/endpoints` and `/api/v1/event-types`. Creating an endpoint returns its
  signing secret exactly once.
- **Dashboard management forms.** The Endpoints page gains a create form (with an
  inline quick-add for event types) and a dedicated Event Types page; a newly
  created endpoint reveals its signing secret once, reusing the rotation reveal.
- **Half-open circuit-breaker auto-recovery.** A disabled endpoint waits out a
  cooldown, is then probed with a single trial delivery (a re-drive of its most
  recent delivery), and re-enables automatically on success or drops back to
  disabled with a fresh cooldown on failure.
- **`Retry-After` honoring.** A `429`/`503` carrying `Retry-After` (delta-seconds
  or HTTP-date) sets the next retry time, clamped to a ceiling and jittered
  upward-only so a retry never lands before the receiver asked.
- **Self-healing `LISTEN/NOTIFY` listener.** The notification listener reconnects
  with capped exponential backoff and wakes the worker on every (re)connect to
  sweep up notifications missed while disconnected.

### Changed

- Endpoint and event-type creation is consolidated into a single shared
  `management` core used by the CLI, the JSON API, and the dashboard, so creation
  rules (validation, secret sealing, subscription wiring) exist in one place.
- The admin CLI's `add-endpoint` and `add-event-type` now delegate to that core
  (and seal secrets at rest like the other surfaces).
- The worker's disabled-endpoint gate now discards only when an endpoint is
  `disabled`, letting a `half_open` endpoint's single trial delivery through.
- Circuit-breaker recovery is automatic by default; manual re-enable plus replay
  remains available as an operator override.

### Security

- A database dump no longer exposes usable signing secrets — forging deliveries
  additionally requires the key-encryption key from the environment.

### Upgrade notes

- Run `alembic upgrade head` to apply the new migrations: sealing existing
  plaintext signing secrets, and widening the `endpoints.status` check constraint
  to allow `half_open`. The encryption key must be present in the environment when
  the sealing migration runs.
- Set and **back up** `STINGER_ENCRYPTION_KEY` like a private key. If unset, a key
  derived from `SECRET_KEY` is used; losing the effective key makes stored signing
  secrets unrecoverable, requiring every receiver to be re-provisioned.

## [0.1.0] — 2026-06-14

Initial release.

### Added

- At-least-once delivery engine: transactional fan-out, lease-based claim via
  `FOR UPDATE SKIP LOCKED`, fixed retry schedule with bounded jitter, and
  crash recovery through visibility-timeout leases.
- HMAC-SHA256 signing, [Standard Webhooks](https://www.standardwebhooks.com)
  compatible, with dual-sign secret rotation.
- SSRF protection: resolve-and-validate against private/loopback/metadata ranges
  with connection pinning to close DNS-rebinding races.
- Circuit breaker that auto-disables consistently failing endpoints, with manual
  re-enable and replay.
- Idempotent publish via an idempotency key, and delivery replay.
- Server-rendered dashboard with a per-delivery attempt timeline, endpoint
  health, and an event log.
- Admin CLI to bootstrap applications, event types, endpoints, and API keys.
- Postgres as the only stateful dependency: the delivery queue is the
  `deliveries` table, inspectable with plain SQL.

[Unreleased]: https://github.com/dylmckay/stinger/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/dylmckay/stinger/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dylmckay/stinger/releases/tag/v0.1.0