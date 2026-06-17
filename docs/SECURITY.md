# Security

## Reporting a vulnerability

Please report privately — **don't open a public issue.** Use GitHub's private
security advisories ("Report a vulnerability" on the repository's Security tab),
or email **dylanmckay2004@gmail.com**. Include reproduction steps and the impact
you've identified. I'll acknowledge the report and work on a fix. This is a
pre-1.0 personal project with no bug bounty, but responsible disclosure is
credited.

## Supported versions

Stinger is pre-1.0; only the latest release receives security fixes. It is young
software — review it yourself before running it on sensitive traffic.

## Security model

What Stinger is designed to defend against, by area.

### Outbound requests (SSRF)

Stinger POSTs to URLs supplied by operators and tenants, which makes the worker
a confused-deputy target: a malicious endpoint URL could aim at internal
services or a cloud metadata endpoint. Before every delivery Stinger:

- **resolves the hostname and validates every resolved IP** against blocked
  ranges — loopback, RFC-1918 private, link-local (which covers the
  `169.254.169.254` metadata address), multicast, reserved, unspecified, and
  IPv4-mapped IPv6 (so `::ffff:127.0.0.1` can't slip through);
- **pins the connection to the validated IP**, closing the DNS-rebinding TOCTOU
  window — an attacker's DNS can't return a safe address for the check and a
  malicious one for the connect;
- **disables HTTP redirects**, since a `3xx` pointing inward is a classic SSRF
  bypass.

The `ALLOW_PRIVATE_TARGETS` flag bypasses the blocklist. It exists for local
testing against localhost receivers and **must remain `false` in production.**

### Webhook signatures

Deliveries are signed with HMAC-SHA256 over `{id}.{timestamp}.{payload}`
(Standard Webhooks compatible). Binding the signature to the message id prevents
a captured signature from being replayed for a different message; binding the
timestamp, combined with a tolerance window on the receiver, prevents it from
being replayed later. Verification uses a constant-time comparison. Secret
rotation runs a dual-sign overlap window so secrets can be rotated without a
verification gap. The consumer-side details are in
[receiving-webhooks.md](receiving-webhooks.md).

### Signing secrets at rest

HMAC signing needs the raw key bytes at delivery time, so signing secrets cannot
be hashed the way API keys are — they must be stored recoverably. Stinger seals
them under **envelope encryption** before writing to Postgres:

- Each secret is encrypted under its own fresh 256-bit data key (DEK) using
  AES-256-GCM.
- The DEK is then wrapped under a key-encryption key (KEK) derived from
  `STINGER_ENCRYPTION_KEY` (or `SECRET_KEY` as a zero-config fallback) via
  HKDF-SHA256 with a scheme-specific info string, producing a key distinct from
  the cookie-signing key even when the same material is used.
- The token stored in Postgres (`stcr.v1.…`) carries the wrapped DEK alongside
  the ciphertext, so the column is self-contained and the KEK never enters the
  database.
- Both layers (DEK-over-plaintext, KEK-over-DEK) use AES-256-GCM, so tampering
  is rejected at open time rather than silently decrypting to garbage.
- The token's header (version + provider id) is the GCM additional authenticated
  data, binding each ciphertext to the scheme that produced it.

The practical consequence: **a database dump no longer exposes signing secrets.**
An attacker with a copy of Postgres cannot forge webhook deliveries your receivers
accept without also obtaining the KEK from your environment.

**The encryption key is load-bearing.** Losing it makes every signing secret
unrecoverable and every receiver would need to be re-provisioned with a new
secret. Back it up with the same care as a private key. If you set
`STINGER_ENCRYPTION_KEY`, ensure it is present in every environment where
`alembic upgrade` runs (the docker-compose `migrate` service reads it from the
same `x-app` env block as `SECRET_KEY`).

A future `KmsKeyProvider` (AWS KMS, Vault, HSM) is a drop-in at the provider
boundary; the local provider is the default because it keeps Postgres as the only
stateful dependency while still closing the database-dump attack.

### API keys

API keys are high-entropy random values, stored **only as a SHA-256 hash** —
never in plaintext. SHA-256 rather than bcrypt is the deliberate and correct
choice for this case: bcrypt's slowness exists to defend *low-entropy* human
passwords, but a 192-bit random key is infeasible to brute-force regardless of
hash speed, and a deterministic hash gives an O(1) indexed lookup that a
per-key salt would make impossible. Keys are shown once at creation, carry an
`sk_` prefix so leaks are detectable by secret scanners, and can be revoked.
There is intentionally no application-side constant-time comparison on lookup:
forging a hash that matches a stored one requires a SHA-256 preimage, so an
indexed equality lookup leaks nothing exploitable.

### Dashboard sessions and tenant isolation

The dashboard has no user/password store. You sign in with an API key, validated
through the same path as the JSON API, and the resolved application id is held in
a session cookie signed with `SECRET_KEY`. Every query — API and dashboard alike
— is scoped to the authenticated application; deliveries and attempts are
reachable only through their event's application id, so one tenant cannot read
another's data. A weak or leaked `SECRET_KEY` permits session forgery, so it must
be set to a strong, unique value.

## Known limitations

Stated plainly. Their absence is a known boundary, not an oversight.

- **No rate limiting on ingest.** A valid API key can publish without throttling;
  protect the ingest endpoint at your proxy if abuse is a concern.
- **HTTP only.** Stinger speaks plain HTTP and does not manage certificates — run
  it behind a TLS-terminating reverse proxy.
- **No 2FA, SSO, or audit log** on the dashboard — access is a single credential,
  the API key.

The broader deferred-work list is in the
[architecture doc](ARCHITECTURE.md#deferred--known-limitations).

## Hardening checklist

- Set a strong, unique `SECRET_KEY`:
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- Set a dedicated `STINGER_ENCRYPTION_KEY` (separate from `SECRET_KEY`) and back
  it up securely. Without it, `SECRET_KEY` is used as the fallback key-encryption
  key — functional, but a single secret then protects both session cookies and
  signing secrets. Generate one the same way as `SECRET_KEY`.
- Keep `ALLOW_PRIVATE_TARGETS=false` in production.
- Run behind a TLS-terminating reverse proxy.
- Restrict network access to Postgres. A database dump no longer exposes signing
  secrets, but the database still holds encrypted tokens and all event/delivery
  data, so access control remains important.
- Rotate endpoint signing secrets periodically (dashboard, or the `rotate-secret`
  action), and rotate API keys as needed.