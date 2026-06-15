"""API-key generation, hashing, and authentication.

Keys are high-entropy random secrets, so we store a fast SHA-256 hash (not
bcrypt): brute-forcing a 192-bit key is infeasible regardless of hash speed,
and a deterministic hash gives an O(1) indexed lookup that a per-row-salted
hash could never provide. The full key is shown ONCE at creation and is
unrecoverable thereafter — we keep only its hash plus a non-secret display
prefix.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiKey, Application

_PREFIX = "sk_"
_DISPLAY_LEN = 11                      # "sk_" + 8 chars, non-secret
_LAST_USED_THROTTLE = timedelta(seconds=60)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Return (full_key, display_prefix, key_hash). full_key is shown once."""
    secret = base64.urlsafe_b64encode(secrets.token_bytes(24)).rstrip(b"=").decode()
    full = f"{_PREFIX}{secret}"
    return full, full[:_DISPLAY_LEN], _hash(full)


async def create_api_key(
    session: AsyncSession, *, application_id: uuid.UUID, name: str | None = None) -> tuple[str, ApiKey]:
    """Mint a key for an application. The returned full key is unrecoverable later."""
    full, prefix, key_hash = generate_api_key()
    row = ApiKey(application_id=application_id, key_hash=key_hash, prefix=prefix, name=name)
    session.add(row)
    await session.commit()
    return full, row


async def revoke_api_key(session: AsyncSession, *, key_id: uuid.UUID) -> None:
    await session.execute(
        update(ApiKey).where(ApiKey.id == key_id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    await session.commit()


async def authenticate(session: AsyncSession, presented_key: str) -> Application | None:
    """Resolve a presented key to its application, or None if invalid/revoked.

    No app-side constant-time compare is needed: forging a matching SHA-256
    requires a preimage of a stored hash, so an indexed equality lookup leaks
    nothing exploitable.
    """
    row = await session.scalar(
        select(ApiKey).where(
            ApiKey.key_hash == _hash(presented_key),
            ApiKey.revoked_at.is_(None),
        )
    )
    if row is None:
        return None

    now = datetime.now(timezone.utc)
    if row.last_used_at is None or now - row.last_used_at > _LAST_USED_THROTTLE:
        await session.execute(
            update(ApiKey).where(ApiKey.id == row.id).values(last_used_at=now)
        )
        await session.commit()

    return await session.scalar(
        select(Application).where(Application.id == row.application_id)
    )