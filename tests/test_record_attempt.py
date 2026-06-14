from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

from app.models import Application, EventType, Endpoint, Event, Delivery, DeliveryAttempt, DeliveryStatus
from app.delivery.claim import claim_deliveries
from app.delivery.record import record_attempt, AttemptResult, RETRY_SCHEDULE


async def _seed_and_claim(factory, n, worker_id="w1"):
    """Create FK parents + n due deliveries, then claim them all as one worker."""
    async with factory() as s:
        app = Application(name="t"); s.add(app); await s.flush()
        et = EventType(application_id=app.id, name="invoice.paid")
        ep = Endpoint(application_id=app.id, url="https://example.test/h", secret="sk")
        s.add_all([et, ep]); await s.flush()
        ev = Event(application_id=app.id, event_type_id=et.id, payload='{"x": 1}')
        s.add(ev); await s.flush()
        s.add_all([Delivery(event_id=ev.id, endpoint_id=ep.id, status=DeliveryStatus.PENDING) for _ in range(n)])
        await s.commit()
    async with factory() as s:
        return list(await claim_deliveries(s, worker_id=worker_id, limit=n))


async def _refresh(factory, did):
    async with factory() as s:
        return (await s.execute(select(Delivery).where(Delivery.id == did))).scalar_one()


async def _attempt_count(factory, did):
    async with factory() as s:
        rows = (await s.execute(
            select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == did)
        )).scalars().all()
    return len(rows)


@pytest.mark.asyncio
async def test_success_marks_succeeded_and_clears_lease(session_factory):
    (d,) = await _seed_and_claim(session_factory, 1)
    async with session_factory() as s:
        ok = await record_attempt(s, delivery=d, worker_id="w1",
                                  result=AttemptResult(succeeded=True, response_status=200))
    r = await _refresh(session_factory, d.id)
    assert ok
    assert r.status == DeliveryStatus.SUCCEEDED
    assert r.attempt_count == 1
    assert r.locked_by is None
    assert await _attempt_count(session_factory, d.id) == 1


@pytest.mark.asyncio
async def test_retryable_failure_schedules_backoff(session_factory):
    (d,) = await _seed_and_claim(session_factory, 1)
    before = datetime.now(timezone.utc)
    async with session_factory() as s:
        ok = await record_attempt(s, delivery=d, worker_id="w1",
                                  result=AttemptResult(succeeded=False, response_status=503))
    r = await _refresh(session_factory, d.id)
    delay = (r.next_attempt_at - before).total_seconds()
    assert ok
    assert r.status == DeliveryStatus.RETRYING
    assert r.attempt_count == 1
    assert 3.0 < delay < 7.0          # 5s base, +/-20% jitter (generous for clock skew)
    assert r.locked_by is None


@pytest.mark.asyncio
async def test_retry_budget_exhausts(session_factory):
    (d,) = await _seed_and_claim(session_factory, 1)
    # Fast-forward to the last scheduled slot rather than looping the real schedule.
    async with session_factory() as s:
        await s.execute(update(Delivery).where(Delivery.id == d.id)
                        .values(attempt_count=len(RETRY_SCHEDULE), locked_by="w1"))
        await s.commit()
    d = await _refresh(session_factory, d.id)
    async with session_factory() as s:
        ok = await record_attempt(s, delivery=d, worker_id="w1",
                                  result=AttemptResult(succeeded=False, response_status=500))
    r = await _refresh(session_factory, d.id)
    assert ok
    assert r.status == DeliveryStatus.EXHAUSTED
    assert r.attempt_count == len(RETRY_SCHEDULE) + 1


@pytest.mark.asyncio
async def test_non_retryable_failure_exhausts_immediately(session_factory):
    (d,) = await _seed_and_claim(session_factory, 1)
    async with session_factory() as s:
        ok = await record_attempt(s, delivery=d, worker_id="w1",
                                  result=AttemptResult(succeeded=False, retryable=False,
                                                       response_status=410))
    r = await _refresh(session_factory, d.id)
    assert ok
    assert r.status == DeliveryStatus.EXHAUSTED
    assert r.attempt_count == 1


@pytest.mark.asyncio
async def test_lost_lease_is_rejected(session_factory):
    (d,) = await _seed_and_claim(session_factory, 1)
    async with session_factory() as s:   # another worker steals the lease
        await s.execute(update(Delivery).where(Delivery.id == d.id).values(locked_by="w2"))
        await s.commit()
    async with session_factory() as s:
        ok = await record_attempt(s, delivery=d, worker_id="w1",
                                  result=AttemptResult(succeeded=True, response_status=200))
    r = await _refresh(session_factory, d.id)
    assert ok is False
    assert r.locked_by == "w2"
    assert await _attempt_count(session_factory, d.id) == 0   # nothing written