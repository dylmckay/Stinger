"""Record the outcome of a delivery attempt, closing the lease.

Second half of the lease lifecycle (claim -> attempt -> record). Runs as one
short transaction and does three things atomically:

  1. Advances the delivery's state machine: succeeded, or retrying with the
     next backoff time, or exhausted once the retry budget is spent.
  2. Appends an immutable DeliveryAttempt row (the audit timeline).
  3. Releases the lease by clearing locked_by, restoring the invariant
     `locked_by IS NOT NULL`  <=>  "a worker holds this row in flight".

A compare-and-swap guard (WHERE locked_by = :worker_id) makes this safe
against the lease-expiry race: if this worker overran its lease and another
worker already re-claimed the row, the UPDATE matches zero rows and we discard
the result rather than clobbering the new owner's state. The HTTP attempt was
at-least-once anyway, so a wasted duplicate POST is expected and handled by
consumer-side dedupe.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Delivery, DeliveryAttempt, DeliveryStatus

# The retry schedule IS the documented delivery contract: one wait per entry,
# applied between attempts. N entries -> N+1 total attempts before exhaustion.
RETRY_SCHEDULE: tuple[timedelta, ...] = (
    timedelta(seconds=5),
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=10),
    timedelta(hours=1),
    timedelta(hours=4),
    timedelta(hours=12),
)
JITTER_FRACTION = 0.2          # +/-20%, de-synchronizes a simultaneous failure batch
MAX_RESPONSE_BODY = 2048       # chars of response body retained on an attempt row


@dataclass(frozen=True)
class AttemptResult:
    """What the HTTP layer observed. It classifies; record_attempt just records.

    `retryable` is consulted only on failure: the HTTP layer sets it False for
    permanent rejections (e.g. 410 Gone) so we stop wasting attempts.
    """
    succeeded: bool
    retryable: bool = True
    response_status: int | None = None
    response_body: str | None = None
    error: str | None = None
    latency_ms: int | None = None


def _jittered(delay: timedelta) -> timedelta:
    return delay * (1.0 + random.uniform(-JITTER_FRACTION, JITTER_FRACTION))


def _truncate(body: str | None) -> str | None:
    return None if body is None else body[:MAX_RESPONSE_BODY]


async def record_attempt(session: AsyncSession, *, delivery: Delivery, worker_id: str, result: AttemptResult) -> bool:
    """Record `result` for `delivery`. Returns False if the lease was lost.

    Reads `delivery.id` and `delivery.attempt_count` (the pre-attempt count,
    reliable because the lease made this worker the row's sole writer).
    """
    new_count = delivery.attempt_count + 1

    if result.succeeded:
        new_status = DeliveryStatus.SUCCEEDED
        next_attempt_at = None
    elif result.retryable and new_count <= len(RETRY_SCHEDULE):
        new_status = DeliveryStatus.RETRYING
        next_attempt_at = func.now() + _jittered(RETRY_SCHEDULE[new_count - 1])
    else:
        new_status = DeliveryStatus.EXHAUSTED
        next_attempt_at = None

    values: dict = {
        "status": new_status,
        "attempt_count": new_count,
        "locked_by": None,   # release the lease; non-null <=> in-flight
    }
    if next_attempt_at is not None:
        values["next_attempt_at"] = next_attempt_at

    # CAS on lease ownership: only the current leaseholder may finalize.
    guard = (
        update(Delivery)
        .where(Delivery.id == delivery.id, Delivery.locked_by == worker_id)
        .values(**values)
        .returning(Delivery.id)
        .execution_options(synchronize_session=False)
    )
    if (await session.execute(guard)).first() is None:
        await session.rollback()
        return False

    session.add(
        DeliveryAttempt(
            delivery_id=delivery.id,
            attempt_number=new_count,
            response_status=result.response_status,
            response_body=_truncate(result.response_body),
            error=result.error,
            latency_ms=result.latency_ms,
        )
    )
    await session.commit()
    return True