"""The claim primitive: atomically lease a batch of due deliveries, bounded by
a per-endpoint in-flight cap.

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

Per-endpoint cap: a single slow endpoint must not monopolise the worker
pool. Each endpoint may have at most `max_concurrent_deliveries` (or the
global default when NULL) deliveries in flight at once. "In flight" is
exactly `locked_by IS NOT NULL` — the same invariant the lease relies on —
so the cap holds across all worker processes with no extra bookkeeping.
"""
from collections.abc import Sequence

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Delivery

# Default per-endpoint in-flight cap when an endpoint sets none. Kept well below
# the worker's global max_concurrency so one endpoint can't fill the pool.
DEFAULT_MAX_CONCURRENT_PER_ENDPOINT = 10

# How many extra due rows to lock as candidates beyond `limit`. Over-fetching
# lets the cap filter skip past a saturated endpoint's head-of-line rows and
# still find admissible work for other endpoints (see _CLAIM_SQL comment).
_CANDIDATE_FANOUT = 4
_CANDIDATE_FLOOR = 200

# Window functions cannot coexist with FOR UPDATE at the same query level in
# Postgres, so locking (candidates) and ranking (ranked) live at separate CTE
# levels. The in-flight count (inflight) is a SEPARATE aggregate read: we must
# NOT lock the already-leased rows or SKIP LOCKED would undercount them.
# Do not collapse `candidates` into `ranked` — Postgres will reject it.
_CLAIM_SQL = text(
    """
    WITH inflight AS (
        SELECT endpoint_id, COUNT(*) AS n
        FROM deliveries
        WHERE locked_by IS NOT NULL
        GROUP BY endpoint_id
    ),
    candidates AS (
        SELECT d.id, d.endpoint_id, d.next_attempt_at
        FROM deliveries d
        WHERE d.status IN ('pending', 'retrying')
          AND d.next_attempt_at <= now()
        ORDER BY d.next_attempt_at, d.id
        LIMIT :candidate_window
        FOR UPDATE SKIP LOCKED
    ),
    ranked AS (
        SELECT
            c.id,
            row_number() OVER (
                PARTITION BY c.endpoint_id
                ORDER BY c.next_attempt_at, c.id
            ) AS rnk,
            COALESCE(i.n, 0) AS inflight_n,
            COALESCE(e.max_concurrent_deliveries, :global_cap) AS cap
        FROM candidates c
        JOIN endpoints e ON e.id = c.endpoint_id
        LEFT JOIN inflight i ON i.endpoint_id = c.endpoint_id
    ),
    admitted AS (
        SELECT id
        FROM ranked
        WHERE rnk + inflight_n <= cap
        ORDER BY rnk
        LIMIT :limit
    )
    UPDATE deliveries
    SET next_attempt_at = now() + (:lease_seconds * interval '1 second'),
        locked_by = :worker_id
    WHERE id IN (SELECT id FROM admitted)
    RETURNING *
    """
)


async def claim_deliveries(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int,
    lease_seconds: int = 30,
    global_endpoint_cap: int = DEFAULT_MAX_CONCURRENT_PER_ENDPOINT,
) -> Sequence[Delivery]:
    """Lease up to `limit` due deliveries for this worker and return them,
    never exceeding any endpoint's in-flight cap.

    Runs as a single short transaction. The caller must process the
    returned rows and record their outcomes BEFORE `lease_seconds`
    elapses, or another worker will reclaim them.

    `lease_seconds` must comfortably exceed the per-attempt HTTP timeout
    (e.g. 30s lease for a 10s request budget) so an in-flight delivery is
    never reclaimed underneath a still-living worker.

    `global_endpoint_cap` is the per-endpoint in-flight cap applied to
    endpoints whose `max_concurrent_deliveries` is NULL.
    """
    if limit <= 0:
        return []

    # Lock more due rows than we intend to claim so the cap filter has
    # alternatives when a saturated endpoint sorts to the front of the queue.
    candidate_window = max(limit * _CANDIDATE_FANOUT, _CANDIDATE_FLOOR)

    claim = (
        select(Delivery)
        .from_statement(
            _CLAIM_SQL.bindparams(
                candidate_window=candidate_window,
                global_cap=global_endpoint_cap,
                limit=limit,
                lease_seconds=lease_seconds,
                worker_id=worker_id,
            )
        )
        .execution_options(synchronize_session=False)
    )

    result = await session.scalars(claim)
    claimed = result.all()

    await session.commit()
    return claimed
