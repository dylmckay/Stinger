import pytest
from sqlalchemy import func, select

from app.models import Application, EventType, Endpoint, EndpointEventType, Event, Delivery
from app.ingest import publish_event, UnknownEventType


async def _setup(factory):
    async with factory() as s:
        app = Application(name="t"); s.add(app); await s.flush()
        paid = EventType(application_id=app.id, name="invoice.paid")
        other = EventType(application_id=app.id, name="user.created")
        s.add_all([paid, other]); await s.flush()
        a = Endpoint(application_id=app.id, url="https://a.test/h", secret="sk", status="enabled")
        b = Endpoint(application_id=app.id, url="https://b.test/h", secret="sk", status="disabled")
        c = Endpoint(application_id=app.id, url="https://c.test/h", secret="sk", status="enabled")
        s.add_all([a, b, c]); await s.flush()
        s.add_all([
            EndpointEventType(endpoint_id=a.id, event_type_id=paid.id),
            EndpointEventType(endpoint_id=b.id, event_type_id=paid.id),    # disabled -> excluded
            EndpointEventType(endpoint_id=c.id, event_type_id=other.id),   # wrong type -> excluded
        ])
        await s.commit()
        return app.id


@pytest.mark.asyncio
async def test_fanout_only_enabled_and_subscribed(session_factory):
    app_id = await _setup(session_factory)
    async with session_factory() as s:
        r = await publish_event(s, application_id=app_id, event_type_name="invoice.paid",
                                payload={"amount": 100}, idempotency_key="k1")
    assert r.delivery_count == 1 and not r.idempotent_replay
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Delivery)) == 1


@pytest.mark.asyncio
async def test_unknown_event_type_raises(session_factory):
    app_id = await _setup(session_factory)
    with pytest.raises(UnknownEventType):
        async with session_factory() as s:
            await publish_event(s, application_id=app_id, event_type_name="nope", payload={})


@pytest.mark.asyncio
async def test_idempotent_replay_does_not_refan(session_factory):
    app_id = await _setup(session_factory)
    async with session_factory() as s:
        first = await publish_event(s, application_id=app_id, event_type_name="invoice.paid",
                                    payload={"amount": 100}, idempotency_key="k1")
    async with session_factory() as s:
        second = await publish_event(s, application_id=app_id, event_type_name="invoice.paid",
                                     payload={"amount": 100}, idempotency_key="k1")
    assert second.idempotent_replay and second.event_id == first.event_id
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Event)) == 1
        assert await s.scalar(select(func.count()).select_from(Delivery)) == 1