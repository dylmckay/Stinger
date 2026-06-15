import hashlib

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app import auth
from app.api import deps
from app.api.events import router
from app.models import Application, EventType, Endpoint, EndpointEventType, ApiKey


async def _seed_app(factory):
    async with factory() as s:
        app = Application(name="acme"); s.add(app); await s.flush()
        et = EventType(application_id=app.id, name="invoice.paid")
        ep = Endpoint(application_id=app.id, url="https://x.test/h", secret="sk", status="enabled")
        s.add_all([et, ep]); await s.flush()
        s.add(EndpointEventType(endpoint_id=ep.id, event_type_id=et.id))
        await s.commit()
        return app.id


def test_generate_format_and_hash():
    full, prefix, key_hash = auth.generate_api_key()
    assert full.startswith("sk_") and prefix == full[:11]
    assert key_hash == hashlib.sha256(full.encode()).hexdigest()


@pytest.mark.asyncio
async def test_create_stores_only_hash(session_factory):
    app_id = await _seed_app(session_factory)
    async with session_factory() as s:
        issued, row = await auth.create_api_key(s, application_id=app_id, name="prod")
    async with session_factory() as s:
        stored = await s.scalar(select(ApiKey).where(ApiKey.id == row.id))
    assert stored.key_hash == hashlib.sha256(issued.encode()).hexdigest()
    assert issued not in (stored.key_hash, stored.prefix)   # full key nowhere in the row


@pytest.mark.asyncio
async def test_authenticate_valid_wrong_revoked(session_factory):
    app_id = await _seed_app(session_factory)
    async with session_factory() as s:
        issued, row = await auth.create_api_key(s, application_id=app_id)
    async with session_factory() as s:
        assert (await auth.authenticate(s, issued)).id == app_id
    async with session_factory() as s:
        assert await auth.authenticate(s, "sk_bogus") is None
    async with session_factory() as s:
        await auth.revoke_api_key(s, key_id=row.id)
        assert await auth.authenticate(s, issued) is None


@pytest.mark.asyncio
async def test_http_requires_valid_key(session_factory):
    app_id = await _seed_app(session_factory)
    async with session_factory() as s:
        issued, _ = await auth.create_api_key(s, application_id=app_id)

    api = FastAPI(); api.include_router(router)
    async def _get_session():
        async with session_factory() as s:
            yield s
    api.dependency_overrides[deps.get_session] = _get_session

    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        body = {"event_type": "invoice.paid", "payload": {"amount": 1}, "idempotency_key": "k1"}
        assert (await client.post("/api/v1/events", json=body)).status_code == 401
        assert (await client.post("/api/v1/events", json=body,
                headers={"Authorization": "Bearer sk_nope"})).status_code == 401
        ok = await client.post("/api/v1/events", json=body,
                               headers={"Authorization": f"Bearer {issued}"})
        assert ok.status_code == 201 and ok.json()["delivery_count"] == 1