"""Per-endpoint concurrency cap in claim_deliveries.

These exercise the SQL directly (no HTTP): claim leases rows out of the due set
by stamping locked_by, and locked_by IS NOT NULL is exactly "in flight", so the
cap is observable purely from what claim returns and what stays leased.
"""
import asyncio
import collections

import pytest

from app.delivery.claim import DEFAULT_MAX_CONCURRENT_PER_ENDPOINT, claim_deliveries
from app.delivery.record import AttemptResult, record_attempt
from app.models import Application, Delivery, DeliveryStatus, Endpoint, Event, EventType


async def _make_parents(factory):
    """One application + event type + event to hang deliveries off of."""
    async with factory() as s:
        app = Application(name="test-app")
        s.add(app)
        await s.flush()
        et = EventType(application_id=app.id, name="invoice.paid")
        s.add(et)
        await s.flush()
        ev = Event(application_id=app.id, event_type_id=et.id, payload='{"amount": 100}')
        s.add(ev)
        await s.commit()
        return app.id, et.id, ev.id


async def _add_endpoint(factory, app_id, *, max_concurrent=None):
    async with factory() as s:
        ep = Endpoint(
            application_id=app_id, url="https://example.test/hook", secret="sk",
            max_concurrent_deliveries=max_concurrent,
        )
        s.add(ep)
        await s.commit()
        return ep.id


async def _add_due(factory, ev_id, ep_id, n):
    """Create n due pending deliveries; return their ids in creation order.

    All share the seed transaction's now() for next_attempt_at, so claim orders
    them by UUIDv7 id — i.e. creation order — which is what the FIFO test asserts.
    """
    async with factory() as s:
        rows = [Delivery(event_id=ev_id, endpoint_id=ep_id, status=DeliveryStatus.PENDING) for _ in range(n)]
        s.add_all(rows)
        await s.flush()
        ids = [r.id for r in rows]
        await s.commit()
        return ids


async def _release_success(factory, delivery, worker_id):
    """Finalize a claimed delivery as succeeded — frees its in-flight slot."""
    async with factory() as s:
        ok = await record_attempt(
            s, delivery=delivery, worker_id=worker_id, result=AttemptResult(succeeded=True)
        )
    assert ok, "lease should still be held when releasing"


@pytest.mark.asyncio
async def test_cap_is_never_exceeded_in_flight(session_factory):
    """An endpoint with cap=3 yields at most 3 in flight, replenishing only as
    earlier deliveries finish."""
    app_id, _, ev_id = await _make_parents(session_factory)
    ep_id = await _add_endpoint(session_factory, app_id, max_concurrent=3)
    await _add_due(session_factory, ev_id, ep_id, 10)

    async with session_factory() as s:
        first = await claim_deliveries(s, worker_id="w1", limit=50)
    assert len(first) == 3
    assert all(d.locked_by == "w1" for d in first)

    # Nothing released yet: the 3 in flight saturate the cap, 0 more admitted.
    async with session_factory() as s:
        again = await claim_deliveries(s, worker_id="w2", limit=50)
    assert again == []

    # Finish one -> exactly one slot frees up.
    await _release_success(session_factory, first[0], "w1")
    async with session_factory() as s:
        third = await claim_deliveries(s, worker_id="w2", limit=50)
    assert len(third) == 1


@pytest.mark.asyncio
async def test_saturated_endpoint_does_not_starve_others(session_factory):
    """A flood on a capped endpoint must not crowd admissible work for others
    out of a single claim batch."""
    app_id, _, ev_id = await _make_parents(session_factory)
    slow = await _add_endpoint(session_factory, app_id, max_concurrent=2)
    fast = await _add_endpoint(session_factory, app_id, max_concurrent=None)

    # `slow` is created first, so its rows sort ahead of `fast`'s in the queue
    # and would fill a naive limit-sized candidate set.
    await _add_due(session_factory, ev_id, slow, 100)
    await _add_due(session_factory, ev_id, fast, 5)

    async with session_factory() as s:
        claimed = await claim_deliveries(s, worker_id="w1", limit=10)

    by_ep = collections.Counter(d.endpoint_id for d in claimed)
    assert by_ep[slow] == 2, "slow endpoint must respect its cap"
    assert by_ep[fast] == 5, "fast endpoint must not be starved by the flood"


@pytest.mark.asyncio
async def test_null_cap_falls_back_to_passed_default(session_factory):
    """A NULL per-endpoint cap uses the global default handed to claim_deliveries."""
    app_id, _, ev_id = await _make_parents(session_factory)
    ep_id = await _add_endpoint(session_factory, app_id, max_concurrent=None)
    await _add_due(session_factory, ev_id, ep_id, 8)

    async with session_factory() as s:
        claimed = await claim_deliveries(s, worker_id="w1", limit=50, global_endpoint_cap=2)
    assert len(claimed) == 2


@pytest.mark.asyncio
async def test_null_cap_uses_module_default_when_unspecified(session_factory):
    """Omitting global_endpoint_cap applies DEFAULT_MAX_CONCURRENT_PER_ENDPOINT."""
    app_id, _, ev_id = await _make_parents(session_factory)
    ep_id = await _add_endpoint(session_factory, app_id, max_concurrent=None)
    await _add_due(session_factory, ev_id, ep_id, DEFAULT_MAX_CONCURRENT_PER_ENDPOINT + 5)

    async with session_factory() as s:
        claimed = await claim_deliveries(s, worker_id="w1", limit=100)
    assert len(claimed) == DEFAULT_MAX_CONCURRENT_PER_ENDPOINT


@pytest.mark.asyncio
async def test_fifo_within_endpoint(session_factory):
    """Within an endpoint the oldest due deliveries are claimed first."""
    app_id, _, ev_id = await _make_parents(session_factory)
    ep_id = await _add_endpoint(session_factory, app_id, max_concurrent=3)
    ids = await _add_due(session_factory, ev_id, ep_id, 6)

    # RETURNING does not order rows, so compare as sets: the property is *which*
    # deliveries are claimed first (the oldest), not their order within the batch.
    async with session_factory() as s:
        batch1 = await claim_deliveries(s, worker_id="w1", limit=50)
    assert {d.id for d in batch1} == set(ids[:3])

    for d in batch1:
        await _release_success(session_factory, d, "w1")

    async with session_factory() as s:
        batch2 = await claim_deliveries(s, worker_id="w1", limit=50)
    assert {d.id for d in batch2} == set(ids[3:])


@pytest.mark.asyncio
async def test_concurrent_claims_disjoint_with_caps(session_factory):
    """Regression: with multiple capped endpoints, N workers still claim each
    due row at most once and never exceed any endpoint's cap."""
    app_id, _, ev_id = await _make_parents(session_factory)
    eps = [await _add_endpoint(session_factory, app_id, max_concurrent=5) for _ in range(4)]
    for ep_id in eps:
        await _add_due(session_factory, ev_id, ep_id, 50)

    sink: list = []

    async def drain(worker_id: str):
        async with session_factory() as s:
            rows = await claim_deliveries(s, worker_id=worker_id, limit=50)
            sink.extend(rows)

    await asyncio.gather(*(drain(f"w{i}") for i in range(6)))

    counts = collections.Counter(d.id for d in sink)
    assert not [k for k, v in counts.items() if v > 1], "a delivery was claimed twice"
    per_ep = collections.Counter(d.endpoint_id for d in sink)
    assert all(n <= 5 for n in per_ep.values()), "an endpoint exceeded its cap"
