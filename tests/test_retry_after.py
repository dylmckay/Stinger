from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
from sqlalchemy import select, update

from app.delivery.claim import claim_deliveries
from app.delivery.http import _parse_retry_after
from app.delivery.record import AttemptResult, RETRY_SCHEDULE, record_attempt
from app.models import Application, Delivery, DeliveryStatus, Endpoint, Event, EventType


# ---- pure parser ----

def test_parse_delta_seconds():
    assert _parse_retry_after("120") == 120.0
    assert _parse_retry_after("  60 ") == 60.0
    assert _parse_retry_after("0") == 0.0


def test_parse_absent_or_garbage():
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("soon") is None
    assert _parse_retry_after("-5") is None        # not valid delta-seconds


def test_parse_http_date_future_and_past():
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    secs = _parse_retry_after(format_datetime(future, usegmt=True))
    assert 3590 < secs <= 3601
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert _parse_retry_after(format_datetime(past, usegmt=True)) == 0.0   # clamped


# ---- DB: honoring in record_attempt ----

async def _seed_and_claim(factory, n, worker_id="w1"):
    async with factory() as s:
        app = Application(name="t"); s.add(app); await s.flush()
        et = EventType(application_id=app.id, name="invoice.paid")
        ep = Endpoint(application_id=app.id, url="https://example.test/h", secret="sk")
        s.add_all([et, ep]); await s.flush()
        ev = Event(application_id=app.id, event_type_id=et.id, payload='{"x": 1}')
        s.add(ev); await s.flush()
        s.add_all([Delivery(event_id=ev.id, endpoint_id=ep.id, status=DeliveryStatus.PENDING)
                   for _ in range(n)])
        await s.commit()
    async with factory() as s:
        return list(await claim_deliveries(s, worker_id=worker_id, limit=n))


async def _refresh(factory, did):
    async with factory() as s:
        return (await s.execute(select(Delivery).where(Delivery.id == did))).scalar_one()


@pytest.mark.asyncio
async def test_retry_after_overrides_backoff(session_factory):
    (d,) = await _seed_and_claim(session_factory, 1)
    before = datetime.now(timezone.utc)
    async with session_factory() as s:
        ok = await record_attempt(
            s, delivery=d, worker_id="w1",
            result=AttemptResult(succeeded=False, response_status=429, retry_after_seconds=60.0),
        )
    r = await _refresh(session_factory, d.id)
    delay = (r.next_attempt_at - before).total_seconds()
    assert ok and r.status == DeliveryStatus.RETRYING
    # honored (≥60s, the receiver's ask) with upward-only jitter; slack for clock skew
    assert 59.0 <= delay <= 74.0


@pytest.mark.asyncio
async def test_retry_after_does_not_extend_budget(session_factory):
    (d,) = await _seed_and_claim(session_factory, 1)
    async with session_factory() as s:               # fast-forward to the last slot
        await s.execute(update(Delivery).where(Delivery.id == d.id)
                        .values(attempt_count=len(RETRY_SCHEDULE), locked_by="w1"))
        await s.commit()
    d = await _refresh(session_factory, d.id)
    async with session_factory() as s:
        ok = await record_attempt(
            s, delivery=d, worker_id="w1",
            result=AttemptResult(succeeded=False, response_status=429, retry_after_seconds=60.0),
        )
    r = await _refresh(session_factory, d.id)
    assert ok and r.status == DeliveryStatus.EXHAUSTED   # budget spent; Retry-After can't extend it