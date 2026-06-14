"""The claim primitive: atomically lease a batch of due deliveries.

Correctness rests on two Postgres mechanics, nothing else:

  1. FOR UPDATE SKIP LOCKED in the subquery — concurrent workers lock
     DISJOINT row sets and skip each other's locked rows, so no delivery
     is ever claimed by two workers at once.
  2. Pushing next_attempt_at into the future (the "visibility timeout"
     lease) — a claimed row falls out of the due-set until the lease
     expires. If the worker finishes, it sets a terminal/real-backoff
     time before the lease runs out. If the worker CRASHES, the lease
     simply expires and the ordinary claim query re-finds the row. No
     reaper process, no separate recovery path.

The row keeps its visible status ('pending'/'retrying') while leased, so
it stays in the ix_deliveries_claim partial index and remains reclaimable.
attempt_count is NOT touched here — it's incremented when an attempt is
actually recorded, so a crash between claim and delivery doesn't burn a
retry that never happened.
"""
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Delivery, DeliveryStatus

_CLAIMABLE = (DeliveryStatus.PENDING, DeliveryStatus.RETRYING)


async def claim_deliveries(session: AsyncSession, *, worker_id: str, limit: int, lease_seconds: int = 30, ) -> Sequence[Delivery]:
    """Lease up to `limit` due deliveries for this worker and return them.

    Runs as a single short transaction. The caller must process the
    returned rows and record their outcomes BEFORE `lease_seconds`
    elapses, or another worker will reclaim them.

    `lease_seconds` must comfortably exceed the per-attempt HTTP timeout
    (e.g. 30s lease for a 10s request budget) so an in-flight delivery is
    never reclaimed underneath a still-living worker.
    """
    if limit <= 0:
        return []

    # 1. Define the query to find due rows
    due_query = (
        select(Delivery.id)
        .where(
            Delivery.status.in_(_CLAIMABLE),
            Delivery.next_attempt_at <= func.now(),
        )
        .order_by(Delivery.next_attempt_at, Delivery.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )

    # 2. Wrap it in a CTE to lock down the execution order & LIMIT behavior
    due_cte = due_query.cte("due_deliveries")

    # 3. Construct the atomic UPDATE query
    claim = (
        update(Delivery)
        .where(Delivery.id.in_(select(due_cte.c.id)))
        .values(
            next_attempt_at=func.now() + timedelta(seconds=lease_seconds),
            locked_by=worker_id,
        )
        .returning(Delivery)
        .execution_options(synchronize_session=False)
    )

    # Use session.scalars() to correctly yield ORM objects from UPDATE RETURNING
    result = await session.scalars(claim)
    claimed = result.all()

    await session.commit()
    return claimed