import uuid

import pytest
from sqlalchemy import func, select

from app import management
from app.crypto import SecretBox
from app.models import Application, Endpoint, EndpointEventType


async def _app(factory, name="acme"):
    async with factory() as s:
        a = Application(name=name); s.add(a); await s.commit()
        return a.id


@pytest.mark.asyncio
async def test_create_event_type_and_duplicate(session_factory):
    app_id = await _app(session_factory)
    async with session_factory() as s:
        et = await management.create_event_type(s, application_id=app_id, name="invoice.paid")
    assert et.name == "invoice.paid"
    async with session_factory() as s:
        with pytest.raises(management.DuplicateEventType):
            await management.create_event_type(s, application_id=app_id, name="invoice.paid")


@pytest.mark.asyncio
async def test_create_event_type_unknown_application(session_factory):
    async with session_factory() as s:
        with pytest.raises(management.ApplicationNotFound):
            await management.create_event_type(s, application_id=uuid.uuid4(), name="x")


@pytest.mark.asyncio
async def test_create_endpoint_seals_secret_and_wires_subscriptions(session_factory, secret_box):
    app_id = await _app(session_factory)
    async with session_factory() as s:
        await management.create_event_type(s, application_id=app_id, name="invoice.paid")
        await management.create_event_type(s, application_id=app_id, name="invoice.failed")
    async with session_factory() as s:
        ep, secret = await management.create_endpoint(
            s, application_id=app_id, url="https://x.test/hook",
            event_type_names=["invoice.paid", "invoice.failed"],
        )
    assert secret.startswith("whsec_")
    async with session_factory() as s:
        stored = await s.get(Endpoint, ep.id)
        # at rest the secret is sealed, never the plaintext key
        assert SecretBox.is_sealed(stored.secret) and secret not in stored.secret
        assert secret_box.open(stored.secret) == secret
        n_subs = await s.scalar(
            select(func.count()).select_from(EndpointEventType)
            .where(EndpointEventType.endpoint_id == ep.id)
        )
    assert n_subs == 2


@pytest.mark.asyncio
async def test_create_endpoint_unknown_event_type_creates_nothing(session_factory):
    app_id = await _app(session_factory)
    async with session_factory() as s:
        await management.create_event_type(s, application_id=app_id, name="invoice.paid")
    async with session_factory() as s:
        with pytest.raises(management.UnknownEventTypes) as ei:
            await management.create_endpoint(
                s, application_id=app_id, url="https://x.test/h",
                event_type_names=["invoice.paid", "nope.missing"],
            )
    assert ei.value.names == ["nope.missing"]
    async with session_factory() as s:                  # rolled back: no endpoint
        assert await s.scalar(select(func.count()).select_from(Endpoint)) == 0


@pytest.mark.asyncio
async def test_event_type_resolution_is_tenant_scoped(session_factory):
    a = await _app(session_factory, "a")
    b = await _app(session_factory, "b")
    async with session_factory() as s:                  # type belongs to tenant b
        await management.create_event_type(s, application_id=b, name="b.only")
    async with session_factory() as s:                  # tenant a can't subscribe to it
        with pytest.raises(management.UnknownEventTypes):
            await management.create_endpoint(
                s, application_id=a, url="https://x.test/h", event_type_names=["b.only"],
            )


@pytest.mark.asyncio
async def test_create_endpoint_rejects_bad_url(session_factory):
    app_id = await _app(session_factory)
    async with session_factory() as s:
        await management.create_event_type(s, application_id=app_id, name="t")
    async with session_factory() as s:
        with pytest.raises(management.InvalidEndpointURL):
            await management.create_endpoint(
                s, application_id=app_id, url="ftp://x/y", event_type_names=["t"],
            )