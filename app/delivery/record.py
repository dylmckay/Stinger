"""Record the outcome of a delivery attempt, closing the lease.

Second half of the lease lifecycle (claim -> attempt -> record). Runs as one
short transaction and does four things atomically:

  1. Advances the delivery's state machine: succeeded, or retrying with the
     next backoff time, or exhausted once the retry budget is spent.
  2. Updates the endpoint's circuit-breaker counter (reset on success,
     increment on failure) and trips it to disabled past the threshold.
  3. Appends an immutable DeliveryAttempt row (the audit timeline).
  4. Releases the lease by clearing locked_by, restoring the invariant
     `locked_by IS NOT NULL`  <=>  "a worker holds this row in flight".

A compare-and-swap guard (WHERE locked_by = :worker_id) makes this safe
against the lease-expiry race: if this worker overran its lease and another
worker already re-claimed the row, the UPDATE matches zero rows and we discard
the result rather than clobbering the new owner's state (and the endpoint
counter is left untouched — the new owner records it). The HTTP attempt was
at-least-once anyway, so a wasted duplicate POST is expected and handled by
consumer-side dedupe.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.delivery import signing
from app.models import Delivery, DeliveryAttempt, DeliveryStatus, Endpoint, EndpointStatus
from app.crypto import get_secret_box

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
JITTER_FRACTION = 0.2           # +/-20%, de-synchronizes a simultaneous failure batch
MIN_RETRY_AFTER_S = 1.0         # floor for an honored Retry-After
MAX_RETRY_AFTER_S = 24 * 60 * 60.0         # ceiling: a receiver can't push retries past 24h
MAX_RESPONSE_BODY = 2048        # chars of response body retained on an attempt row
DEFAULT_FAILURE_THRESHOLD = 20  # consecutive endpoint failures before auto-disable
DEFAULT_ROTATION_WINDOW = timedelta(hours=24)  # overlap window where both secrets sign


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
    retry_after_seconds: float | None = None


def _jittered(delay: timedelta) -> timedelta:
    return delay * (1.0 + random.uniform(-JITTER_FRACTION, JITTER_FRACTION))

def _next_retry_delay(attempt_count: int, retry_after_seconds: float | None) -> timedelta:
    """Delay before the next attempt. A receiver's Retry-After (429/503) overrides
    the schedule for TIMING only — clamped to a ceiling so a receiver can't push
    retries arbitrarily far, and jittered upward-only so we never come back before
    the time it asked for. The retry BUDGET is unchanged: Retry-After moves *when*,
    not *whether*, and the attempt still counts toward exhaustion."""
    if retry_after_seconds is not None:
        secs = min(max(retry_after_seconds, MIN_RETRY_AFTER_S), MAX_RETRY_AFTER_S)
        return timedelta(seconds=secs * (1.0 + random.uniform(0.0, JITTER_FRACTION)))
    return _jittered(RETRY_SCHEDULE[attempt_count - 1])

def _truncate(body: str | None) -> str | None:
    return None if body is None else body[:MAX_RESPONSE_BODY]


async def record_attempt(
    session: AsyncSession,
    *,
    delivery: Delivery,
    worker_id: str,
    result: AttemptResult,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
) -> bool:
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
        next_attempt_at = func.now() + _next_retry_delay(new_count, result.retry_after_seconds)
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

    # --- circuit breaker: same transaction as the outcome ---
    if result.succeeded:
        await session.execute(
            update(Endpoint)
            .where(Endpoint.id == delivery.endpoint_id)
            .values(consecutive_failures=0)
        )
    else:
        bumped = (await session.execute(
            update(Endpoint)
            .where(Endpoint.id == delivery.endpoint_id)
            .values(consecutive_failures=Endpoint.consecutive_failures + 1)
            .returning(Endpoint.consecutive_failures)
        )).scalar_one()
        if bumped >= failure_threshold:
            # One-time trip: WHERE status='enabled' so concurrent failures
            # crossing the threshold together disable exactly once and stamp
            # disabled_at exactly once.
            await session.execute(
                update(Endpoint)
                .where(
                    Endpoint.id == delivery.endpoint_id,
                    Endpoint.status == EndpointStatus.ENABLED,
                )
                .values(status=EndpointStatus.DISABLED, disabled_at=func.now())
            )

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


async def discard_delivery(
    session: AsyncSession, *, delivery: Delivery, worker_id: str
) -> bool:
    """Void a leased delivery without attempting it (endpoint is disabled).

    Same CAS guard as record_attempt; writes no attempt row, since nothing was
    sent. Returns False if the lease was lost.
    """
    stmt = (
        update(Delivery)
        .where(Delivery.id == delivery.id, Delivery.locked_by == worker_id)
        .values(status=DeliveryStatus.DISCARDED, locked_by=None)
        .returning(Delivery.id)
        .execution_options(synchronize_session=False)
    )
    if (await session.execute(stmt)).first() is None:
        await session.rollback()
        return False
    await session.commit()
    return True


async def reenable_endpoint(
    session: AsyncSession, *, application_id, endpoint_id
) -> bool:
    """Manually re-enable a disabled endpoint, resetting the breaker counter."""
    found = (await session.execute(
        update(Endpoint)
        .where(Endpoint.id == endpoint_id, Endpoint.application_id == application_id)
        .values(
            status=EndpointStatus.ENABLED,
            disabled_at=None,
            consecutive_failures=0,
        )
        .returning(Endpoint.id)
    )).first()
    await session.commit()
    return found is not None


async def rotate_endpoint_secret(
    session: AsyncSession,
    *,
    application_id,
    endpoint_id,
    window: timedelta = DEFAULT_ROTATION_WINDOW,
) -> str | None:
    """Rotate an endpoint's signing secret, opening a dual-sign overlap window.

    Moves the current secret to `previous_secret` and stamps
    `previous_secret_expires_at = now() + window`, so the worker signs with BOTH
    until the window closes and consumers can migrate without a verification gap.
    Returns the new secret (shown once) or None if the endpoint isn't found.

    The SET right-hand sides evaluate against the pre-update row, so
    `previous_secret = secret` captures the OLD secret in the same statement that
    installs the new one.
    """
    new_secret = signing.generate_secret()
    found = (await session.execute(
        update(Endpoint)
        .where(Endpoint.id == endpoint_id, Endpoint.application_id == application_id)
        .values(
            previous_secret=Endpoint.secret,
            secret=get_secret_box().seal(new_secret),
            previous_secret_expires_at=func.now() + window,
        )
        .returning(Endpoint.id)
    )).first()
    await session.commit()
    return new_secret if found is not None else None