import pytest

from app import reads
from app.models import Application, EventType, Endpoint, EndpointEventType, Event, Delivery, DeliveryAttempt


async def _seed(factory, n_deliveries=25):
    async with factory() as s:
        a = Application(name="A"); b = Application(name="B"); s.add_all([a, b]); await s.flush()
        et = EventType(application_id=a.id, name="invoice.paid"); s.add(et); await s.flush()
        ep = Endpoint(application_id=a.id, url="https://x.test/h", secret="sk", status="enabled")
        s.add(ep); await s.flush()
        s.add(EndpointEventType(endpoint_id=ep.id, event_type_id=et.id))
        first = None
        for i in range(n_deliveries):
            ev = Event(application_id=a.id, event_type_id=et.id, payload=f'{{"i":{i}}}')
            s.add(ev); await s.flush()
            d = Delivery(event_id=ev.id, endpoint_id=ep.id, status="exhausted")
            s.add(d); await s.flush()
            first = first or d.id
        await s.commit()
        return a.id, b.id, first


@pytest.mark.asyncio
async def test_pagination_is_complete_and_unique(session_factory):
    app_id, _, _ = await _seed(session_factory, 25)
    seen, cursor = [], None
    async with session_factory() as s:
        while True:
            page = await reads.list_deliveries(s, application_id=app_id, limit=10, cursor=cursor)
            seen.extend(d.id for d in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
    assert len(seen) == 25 and len(set(seen)) == 25           # complete, no dupes
    assert [int(x) for x in seen] == sorted((int(x) for x in seen), reverse=True)  # newest-first


@pytest.mark.asyncio
async def test_tenant_isolation(session_factory):
    app_a, app_b, first = await _seed(session_factory)
    async with session_factory() as s:
        assert await reads.get_delivery_detail(s, application_id=app_a, delivery_id=first) is not None
        assert await reads.get_delivery_detail(s, application_id=app_b, delivery_id=first) is None


@pytest.mark.asyncio
async def test_detail_timeline_ordered(session_factory):
    app_a, _, first = await _seed(session_factory)
    async with session_factory() as s:
        for n in (2, 1, 3):                                   # inserted out of order
            s.add(DeliveryAttempt(delivery_id=first, attempt_number=n))
        await s.commit()
    async with session_factory() as s:
        detail = await reads.get_delivery_detail(s, application_id=app_a, delivery_id=first)
    assert [a.attempt_number for a in detail.attempts] == [1, 2, 3]


@pytest.mark.asyncio
async def test_replay_creates_fresh_delivery_leaves_original(session_factory):
    app_a, _, first = await _seed(session_factory)
    async with session_factory() as s:
        new_id = await reads.replay_delivery(s, application_id=app_a, delivery_id=first)
    assert new_id != first
    async with session_factory() as s:
        replayed = await s.get(Delivery, new_id)
        original = await s.get(Delivery, first)
    assert replayed.status == "pending" and replayed.attempt_count == 0
    assert original.status == "exhausted"                     # untouched


@pytest.mark.asyncio
async def test_replay_cross_tenant_denied(session_factory):
    _, app_b, first = await _seed(session_factory)
    async with session_factory() as s:
        assert await reads.replay_delivery(s, application_id=app_b, delivery_id=first) is None