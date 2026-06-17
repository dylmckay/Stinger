from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from app.delivery.claim import claim_deliveries
from app.delivery.record import AttemptResult, promote_half_open_endpoints, record_attempt
from app.models import Application, Delivery, DeliveryStatus, Endpoint, EndpointStatus, Event, EventType

COOLDOWN = timedelta(minutes=5)


async def _seed(factory, *, disabled_ago, with_history=True):
    """An endpoint left 'disabled' `disabled_ago` in the past, with (by default)
    one prior terminal delivery for the sweep to re-drive as the trial."""
    async with factory() as s:
        app = Application(name="t"); s.add(app); await s.flush()
        et = EventType(application_id=app.id, name="invoice.paid")
        ep = Endpoint(application_id=app.id, url="https://x.test/h", secret="sk", status=EndpointStatus.DISABLED, consecutive_failures=20)
        s.add_all([et, ep]); await s.flush()
        ev = Event(application_id=app.id, event_type_id=et.id, payload='{"x":1}')
        s.add(ev); await s.flush()
        if with_history:
            s.add(Delivery(event_id=ev.id, endpoint_id=ep.id, status=DeliveryStatus.DISCARDED))
        await s.commit()
        ep_id, ev_id = ep.id, ev.id
    async with factory() as s:               # backdate disabled_at via the DB clock
        await s.execute(update(Endpoint).where(Endpoint.id == ep_id)
                        .values(disabled_at=func.now() - disabled_ago))
        await s.commit()
    return ep_id, ev_id


async def _ep(factory, ep_id):
    async with factory() as s:
        return await s.get(Endpoint, ep_id)


async def _pending(factory, ep_id):
    async with factory() as s:
        return list(await s.scalars(select(Delivery).where(
            Delivery.endpoint_id == ep_id, Delivery.status == DeliveryStatus.PENDING)))


@pytest.mark.asyncio
async def test_promote_after_cooldown_enqueues_one_trial(session_factory):
    ep_id, ev_id = await _seed(session_factory, disabled_ago=timedelta(minutes=10))
    async with session_factory() as s:
        n = await promote_half_open_endpoints(s, cooldown=COOLDOWN)
    assert n == 1
    assert (await _ep(session_factory, ep_id)).status == EndpointStatus.HALF_OPEN
    trials = await _pending(session_factory, ep_id)
    assert len(trials) == 1 and trials[0].event_id == ev_id


@pytest.mark.asyncio
async def test_promote_respects_cooldown(session_factory):
    ep_id, _ = await _seed(session_factory, disabled_ago=timedelta(minutes=1))
    async with session_factory() as s:
        n = await promote_half_open_endpoints(s, cooldown=COOLDOWN)
    assert n == 0
    assert (await _ep(session_factory, ep_id)).status == EndpointStatus.DISABLED
    assert await _pending(session_factory, ep_id) == []


@pytest.mark.asyncio
async def test_promote_is_idempotent_single_trial(session_factory):
    ep_id, _ = await _seed(session_factory, disabled_ago=timedelta(minutes=10))
    async with session_factory() as s:
        assert await promote_half_open_endpoints(s, cooldown=COOLDOWN) == 1
    async with session_factory() as s:                 # already half_open -> no-op
        assert await promote_half_open_endpoints(s, cooldown=COOLDOWN) == 0
    assert len(await _pending(session_factory, ep_id)) == 1


@pytest.mark.asyncio
async def test_trial_success_reenables(session_factory):
    ep_id, _ = await _seed(session_factory, disabled_ago=timedelta(minutes=10))
    async with session_factory() as s:
        await promote_half_open_endpoints(s, cooldown=COOLDOWN)
    async with session_factory() as s:
        (trial,) = list(await claim_deliveries(s, worker_id="w1", limit=10))
    async with session_factory() as s:
        await record_attempt(s, delivery=trial, worker_id="w1", result=AttemptResult(succeeded=True, response_status=200))
    ep = await _ep(session_factory, ep_id)
    assert ep.status == EndpointStatus.ENABLED
    assert ep.disabled_at is None and ep.consecutive_failures == 0


@pytest.mark.asyncio
async def test_trial_failure_redisables_with_fresh_cooldown(session_factory):
    ep_id, _ = await _seed(session_factory, disabled_ago=timedelta(minutes=10))
    async with session_factory() as s:
        await promote_half_open_endpoints(s, cooldown=COOLDOWN)
    async with session_factory() as s:
        (trial,) = list(await claim_deliveries(s, worker_id="w1", limit=10))
    before = datetime.now(timezone.utc)
    async with session_factory() as s:
        await record_attempt(s, delivery=trial, worker_id="w1", result=AttemptResult(succeeded=False, response_status=500))
    ep = await _ep(session_factory, ep_id)
    assert ep.status == EndpointStatus.DISABLED
    da = ep.disabled_at
    if da.tzinfo is None:
        da = da.replace(tzinfo=timezone.utc)
    assert abs((da - before).total_seconds()) < 60          # fresh stamp, not the old one


@pytest.mark.asyncio
async def test_promote_with_no_history_reenables(session_factory):
    ep_id, _ = await _seed(session_factory, disabled_ago=timedelta(minutes=10), with_history=False)
    async with session_factory() as s:
        n = await promote_half_open_endpoints(s, cooldown=COOLDOWN)
    assert n == 0                                           # nothing to probe -> no trial
    assert (await _ep(session_factory, ep_id)).status == EndpointStatus.ENABLED
    assert await _pending(session_factory, ep_id) == []