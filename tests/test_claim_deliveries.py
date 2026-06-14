import asyncio
import collections
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Application, EventType, Endpoint, Event, Delivery, DeliveryStatus
from app.delivery.claim import claim_deliveries


async def _seed(factory, *, n_due=0, n_future=0, n_done=0):
    """Create the FK parents, then a mix of due / future / terminal deliveries."""
    async with factory() as s:
        app = Application(name="test-app")
        s.add(app)
        await s.flush()
        et = EventType(application_id=app.id, name="invoice.paid")
        ep = Endpoint(application_id=app.id, url="https://example.test/hook", secret="sk")
        s.add_all([et, ep])
        await s.flush()
        ev = Event(application_id=app.id, event_type_id=et.id, payload='{"amount": 100}')
        s.add(ev)
        await s.flush()

        future = datetime.now(timezone.utc) + timedelta(hours=1)
        rows = [Delivery(event_id=ev.id, endpoint_id=ep.id, status=DeliveryStatus.PENDING) for _ in range(n_due)]
        rows += [Delivery(event_id=ev.id, endpoint_id=ep.id, status=DeliveryStatus.RETRYING, next_attempt_at=future) for _ in range(n_future)]
        rows += [Delivery(event_id=ev.id, endpoint_id=ep.id, status=DeliveryStatus.SUCCEEDED) for _ in range(n_done)]
        s.add_all(rows)
        await s.commit()


@pytest.mark.asyncio
async def test_concurrent_claims_are_disjoint_and_complete(session_factory):
    """The core property: N workers claim every due row exactly once, none twice."""
    N_DUE, N_WORKERS, BATCH = 1000, 8, 50
    await _seed(session_factory, n_due=N_DUE)

    sink: list = []

    async def drain(worker_id: str):
        async with session_factory() as s:  # each worker gets its own session
            while True:
                rows = await claim_deliveries(s, worker_id=worker_id, limit=BATCH)
                if not rows:
                    return
                sink.extend(d.id for d in rows)

    await asyncio.gather(*(drain(f"w{i}") for i in range(N_WORKERS)))

    counts = collections.Counter(sink)
    assert not [k for k, v in counts.items() if v > 1], "a delivery was claimed twice"
    assert len(set(sink)) == N_DUE, "some deliveries were never claimed"


@pytest.mark.asyncio
async def test_claim_leases_rows_out_of_the_due_set(session_factory):
    """A claimed row is leased into the future and invisible to the next claim."""
    await _seed(session_factory, n_due=10)

    async with session_factory() as s:
        first = await claim_deliveries(s, worker_id="w1", limit=10)
    assert len(first) == 10
    assert all(d.locked_by == "w1" for d in first)

    async with session_factory() as s:
        second = await claim_deliveries(s, worker_id="w2", limit=10)
    assert second == []  # everything is leased; nothing is due


@pytest.mark.asyncio
async def test_claim_ignores_future_and_terminal_rows(session_factory):
    """Only due pending/retrying rows are claimable - not future, not terminal."""
    await _seed(session_factory, n_due=5, n_future=5, n_done=5)

    async with session_factory() as s:
        claimed = await claim_deliveries(s, worker_id="w1", limit=100)
    assert len(claimed) == 5