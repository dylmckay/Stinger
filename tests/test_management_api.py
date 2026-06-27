import httpx
import pytest
from fastapi import FastAPI

from app import auth
from app.api import deps
from app.api.management import router
from app.models import Application


async def _seed(factory, name="acme"):
    async with factory() as s:
        a = Application(name=name); s.add(a); await s.commit()
        return a.id


async def _key(factory, application_id):
    async with factory() as s:
        issued, _ = await auth.create_api_key(s, application_id=application_id)
    return issued


def _client(factory):
    api = FastAPI(); api.include_router(router)
    async def _get_session():
        async with factory() as s:
            yield s
    api.dependency_overrides[deps.get_session] = _get_session
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://t")


@pytest.mark.asyncio
async def test_requires_auth(session_factory):
    await _seed(session_factory)
    async with _client(session_factory) as c:
        r = await c.post("/api/v1/event-types", json={"name": "x"})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_event_type_create_list_and_duplicate(session_factory):
    app_id = await _seed(session_factory)
    h = {"Authorization": f"Bearer {await _key(session_factory, app_id)}"}
    async with _client(session_factory) as c:
        r = await c.post("/api/v1/event-types", json={"name": "invoice.paid"}, headers=h)
        assert r.status_code == 201 and r.json()["name"] == "invoice.paid"
        dup = await c.post("/api/v1/event-types", json={"name": "invoice.paid"}, headers=h)
        assert dup.status_code == 409
        lst = await c.get("/api/v1/event-types", headers=h)
        assert [e["name"] for e in lst.json()["items"]] == ["invoice.paid"]


@pytest.mark.asyncio
async def test_endpoint_create_returns_secret_once_and_lists(session_factory):
    app_id = await _seed(session_factory)
    h = {"Authorization": f"Bearer {await _key(session_factory, app_id)}"}
    async with _client(session_factory) as c:
        await c.post("/api/v1/event-types", json={"name": "invoice.paid"}, headers=h)
        r = await c.post(
            "/api/v1/endpoints",
            json={"url": "https://x.test/hook", "event_types": ["invoice.paid"]},
            headers=h,
        )
        assert r.status_code == 201
        body = r.json()
        assert body["secret"].startswith("whsec_")
        assert body["event_types"] == ["invoice.paid"]
        secret = body["secret"]

        lst = await c.get("/api/v1/endpoints", headers=h)
        items = lst.json()["items"]
        assert len(items) == 1 and items[0]["url"] == "https://x.test/hook"
        assert "secret" not in items[0]        # the secret is never returned again
        assert secret not in lst.text


@pytest.mark.asyncio
async def test_endpoint_unknown_event_type_is_422_with_names(session_factory):
    app_id = await _seed(session_factory)
    h = {"Authorization": f"Bearer {await _key(session_factory, app_id)}"}
    async with _client(session_factory) as c:
        r = await c.post(
            "/api/v1/endpoints",
            json={"url": "https://x.test/h", "event_types": ["nope"]},
            headers=h,
        )
        assert r.status_code == 422
        assert "nope" in r.json()["detail"]["unknown_event_types"]


@pytest.mark.asyncio
async def test_endpoint_requires_at_least_one_event_type(session_factory):
    app_id = await _seed(session_factory)
    h = {"Authorization": f"Bearer {await _key(session_factory, app_id)}"}
    async with _client(session_factory) as c:
        r = await c.post(
            "/api/v1/endpoints",
            json={"url": "https://x.test/h", "event_types": []},
            headers=h,
        )
        assert r.status_code == 422        # Pydantic min_length=1


async def _create_endpoint(c, h, *, cap=None):
    await c.post("/api/v1/event-types", json={"name": "invoice.paid"}, headers=h)
    body = {"url": "https://x.test/hook", "event_types": ["invoice.paid"]}
    if cap is not None:
        body["max_concurrent_deliveries"] = cap
    r = await c.post("/api/v1/endpoints", json=body, headers=h)
    assert r.status_code == 201
    return r.json()["id"]


@pytest.mark.asyncio
async def test_endpoint_patch_max_concurrent(session_factory):
    app_id = await _seed(session_factory)
    h = {"Authorization": f"Bearer {await _key(session_factory, app_id)}"}
    async with _client(session_factory) as c:
        ep_id = await _create_endpoint(c, h, cap=5)
        r = await c.patch(
            f"/api/v1/endpoints/{ep_id}",
            json={"max_concurrent_deliveries": 3},
            headers=h,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["max_concurrent_deliveries"] == 3
        assert body["event_types"] == ["invoice.paid"]      # full EndpointOut shape

        lst = await c.get("/api/v1/endpoints", headers=h)
        assert lst.json()["items"][0]["max_concurrent_deliveries"] == 3


@pytest.mark.asyncio
async def test_endpoint_patch_null_resets_to_default(session_factory):
    app_id = await _seed(session_factory)
    h = {"Authorization": f"Bearer {await _key(session_factory, app_id)}"}
    async with _client(session_factory) as c:
        ep_id = await _create_endpoint(c, h, cap=5)
        r = await c.patch(
            f"/api/v1/endpoints/{ep_id}",
            json={"max_concurrent_deliveries": None},
            headers=h,
        )
        assert r.status_code == 200
        assert r.json()["max_concurrent_deliveries"] is None


@pytest.mark.asyncio
async def test_endpoint_patch_rejects_non_positive(session_factory):
    app_id = await _seed(session_factory)
    h = {"Authorization": f"Bearer {await _key(session_factory, app_id)}"}
    async with _client(session_factory) as c:
        ep_id = await _create_endpoint(c, h)
        r = await c.patch(
            f"/api/v1/endpoints/{ep_id}",
            json={"max_concurrent_deliveries": 0},
            headers=h,
        )
        assert r.status_code == 422        # Pydantic ge=1


@pytest.mark.asyncio
async def test_endpoint_patch_unknown_id_is_404(session_factory):
    app_id = await _seed(session_factory)
    h = {"Authorization": f"Bearer {await _key(session_factory, app_id)}"}
    async with _client(session_factory) as c:
        import uuid
        r = await c.patch(
            f"/api/v1/endpoints/{uuid.uuid4()}",
            json={"max_concurrent_deliveries": 3},
            headers=h,
        )
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_endpoint_patch_is_tenant_scoped(session_factory):
    app_a = await _seed(session_factory, name="a")
    app_b = await _seed(session_factory, name="b")
    ha = {"Authorization": f"Bearer {await _key(session_factory, app_a)}"}
    hb = {"Authorization": f"Bearer {await _key(session_factory, app_b)}"}
    async with _client(session_factory) as c:
        ep_id = await _create_endpoint(c, ha, cap=5)
        # B must not be able to retune A's endpoint.
        r = await c.patch(
            f"/api/v1/endpoints/{ep_id}",
            json={"max_concurrent_deliveries": 99},
            headers=hb,
        )
        assert r.status_code == 404