import re
from contextlib import asynccontextmanager
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import auth
from app.models import Application
from app.web.app import create_web_app
from app.web.deps import csrf_token, verify_csrf



def _request(*, session=None, headers=None, form_body=b""):
    """Build a minimal Starlette Request with an injected session dict."""
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if form_body:
        raw_headers.append((b"content-type", b"application/x-www-form-urlencoded"))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        "headers": raw_headers,
        "session": dict(session or {}),
    }

    async def receive():
        return {"type": "http.request", "body": form_body, "more_body": False}

    return Request(scope, receive)


def test_csrf_token_generates_and_stores():
    req = _request()
    token = csrf_token(req)
    assert token
    assert req.session["_csrf_token"] == token


def test_csrf_token_stable_across_calls():
    req = _request()
    assert csrf_token(req) == csrf_token(req)


def test_csrf_token_returns_existing():
    req = _request(session={"_csrf_token": "pre-existing"})
    assert csrf_token(req) == "pre-existing"


def test_csrf_token_unique_per_new_session():
    assert csrf_token(_request()) != csrf_token(_request())


@pytest.mark.asyncio
async def test_verify_csrf_no_session_token_is_403():
    with pytest.raises(HTTPException) as exc:
        await verify_csrf(_request())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_verify_csrf_wrong_header_is_403():
    req = _request(session={"_csrf_token": "correct"}, headers={"X-CSRF-Token": "wrong"})
    with pytest.raises(HTTPException) as exc:
        await verify_csrf(req)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_verify_csrf_wrong_form_token_is_403():
    body = urlencode({"csrf_token": "wrong"}).encode()
    req = _request(session={"_csrf_token": "correct"}, form_body=body)
    with pytest.raises(HTTPException) as exc:
        await verify_csrf(req)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_verify_csrf_no_token_provided_is_403():
    req = _request(session={"_csrf_token": "correct"})
    with pytest.raises(HTTPException) as exc:
        await verify_csrf(req)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_verify_csrf_valid_header_passes():
    req = _request(session={"_csrf_token": "tok"}, headers={"X-CSRF-Token": "tok"})
    await verify_csrf(req)  # must not raise


@pytest.mark.asyncio
async def test_verify_csrf_valid_form_token_passes():
    body = urlencode({"csrf_token": "tok"}).encode()
    req = _request(session={"_csrf_token": "tok"}, form_body=body)
    await verify_csrf(req)  # must not raise


async def _seed(factory, name="acme"):
    async with factory() as s:
        a = Application(name=name)
        s.add(a)
        await s.commit()
        return a.id


async def _issue_key(factory, application_id):
    async with factory() as s:
        issued, _ = await auth.create_api_key(s, application_id=application_id)
    return issued


def _web_client(session_factory):
    app = create_web_app(session_factory, secret_key="test-secret-key")
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    )


def _extract_csrf(html: str) -> str:
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m, "csrf_token hidden input not found in HTML"
    return m.group(1)


@asynccontextmanager
async def _authenticated_client(session_factory):
    """Async context manager yielding (client, csrf_token) with a live session."""
    app_id = await _seed(session_factory)
    key = await _issue_key(session_factory, app_id)
    async with _web_client(session_factory) as c:
        login_page = await c.get("/login")
        token = _extract_csrf(login_page.text)
        await c.post("/login", data={"api_key": key, "csrf_token": token})
        yield c, token


@pytest.mark.asyncio
async def test_login_page_renders_csrf_hidden_input(session_factory):
    async with _web_client(session_factory) as c:
        r = await c.get("/login")
    assert r.status_code == 200
    _extract_csrf(r.text)


@pytest.mark.asyncio
async def test_login_post_without_csrf_is_403(session_factory):
    async with _web_client(session_factory) as c:
        await c.get("/login")  # seed the session cookie
        r = await c.post("/login", data={"api_key": "sk_anything"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_login_post_with_wrong_csrf_is_403(session_factory):
    async with _web_client(session_factory) as c:
        await c.get("/login")
        r = await c.post("/login", data={"api_key": "sk_anything", "csrf_token": "wrong"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_login_post_with_valid_csrf_redirects(session_factory):
    app_id = await _seed(session_factory)
    key = await _issue_key(session_factory, app_id)
    async with _web_client(session_factory) as c:
        login_page = await c.get("/login")
        token = _extract_csrf(login_page.text)
        r = await c.post("/login", data={"api_key": key, "csrf_token": token})
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/deliveries"


@pytest.mark.asyncio
async def test_dashboard_post_without_csrf_is_403(session_factory):
    async with _authenticated_client(session_factory) as (c, _):
        r = await c.post("/dashboard/event-types", data={"name": "invoice.paid"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_dashboard_post_with_csrf_header_succeeds(session_factory):
    # Simulates HTMX sending X-CSRF-Token via hx-headers on <body>
    async with _authenticated_client(session_factory) as (c, token):
        r = await c.post(
            "/dashboard/event-types",
            data={"name": "invoice.paid"},
            headers={"X-CSRF-Token": token},
        )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_dashboard_post_with_csrf_form_field_succeeds(session_factory):
    # Simulates a form submission with the hidden csrf_token input
    async with _authenticated_client(session_factory) as (c, token):
        r = await c.post(
            "/dashboard/event-types",
            data={"name": "order.created", "csrf_token": token},
        )
    assert r.status_code == 200
