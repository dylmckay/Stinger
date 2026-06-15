"""HMAC-SHA256 webhook signing, compatible with the Standard Webhooks spec.

Each delivery carries three headers:
  webhook-id         the message id (also used for consumer-side dedupe)
  webhook-timestamp  unix seconds; lets the consumer reject stale replays
  webhook-signature  one or more space-separated `v1,<base64>` tokens

The signed content is `{id}.{timestamp}.{payload}` — binding the signature to
the message id (so a valid signature can't be replayed for a different message)
and the timestamp (so it can't be replayed later), over the exact payload bytes
we send. During secret rotation we emit a token for BOTH the active and the
previous secret; the consumer accepts if any verifies.

Matches standardwebhooks.com, so consumers can verify with off-the-shelf
libraries in any language.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as _secrets
import time
from collections.abc import Mapping

_PREFIX = "whsec_"
_VERSION = "v1"


def generate_secret(n_bytes: int = 24) -> str:
    """A fresh signing secret: whsec_ + base64(random bytes)."""
    return _PREFIX + base64.b64encode(_secrets.token_bytes(n_bytes)).decode()


def _key(secret: str) -> bytes:
    raw = secret[len(_PREFIX):] if secret.startswith(_PREFIX) else secret
    return base64.b64decode(raw)


def _sig(secret: str, message_id: str, timestamp: int, payload: str) -> str:
    signed = f"{message_id}.{timestamp}.{payload}".encode()
    mac = hmac.new(_key(secret), signed, hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def sign(payload: str, *, message_id: str, secret: str, previous_secret: str | None = None, timestamp: int | None = None) -> dict[str, str]:
    """Produce the webhook-* headers for a signed delivery.

    Pass `previous_secret` during a rotation window to emit a second token.
    """
    ts = int(time.time()) if timestamp is None else timestamp
    tokens = [f"{_VERSION},{_sig(secret, message_id, ts, payload)}"]
    if previous_secret:
        tokens.append(f"{_VERSION},{_sig(previous_secret, message_id, ts, payload)}")
    return {
        "webhook-id": message_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": " ".join(tokens),
    }


def verify(payload: str, headers: Mapping[str, str], *, secret: str, tolerance_seconds: int = 300) -> bool:
    """Consumer-side verification: constant-time, with replay tolerance."""
    try:
        message_id = headers["webhook-id"]
        ts = int(headers["webhook-timestamp"])
        header_tokens = headers["webhook-signature"].split()
    except (KeyError, ValueError):
        return False

    if abs(time.time() - ts) > tolerance_seconds:
        return False

    expected = _sig(secret, message_id, ts, payload)
    for token in header_tokens:
        _, _, sig = token.partition(",")
        if hmac.compare_digest(sig, expected):
            return True
    return False