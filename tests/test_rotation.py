from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from app.crypto import get_secret_box
from app.delivery import signing
from app.delivery.record import rotate_endpoint_secret
from app.models import Application, Endpoint


async def _seed(factory):
    old = signing.generate_secret()
    box = get_secret_box()
    async with factory() as s:
        a = Application(name="A"); b = Application(name="B"); s.add_all([a, b]); await s.flush()
        ep = Endpoint(application_id=a.id, url="https://x.test/h", secret=box.seal(old), status="enabled")
        s.add(ep); await s.flush()
        await s.commit()
        return a.id, b.id, ep.id, old


@pytest.mark.asyncio
async def test_rotation_sets_window_and_dual_signs(session_factory):
    app_a, _, ep_id, old = await _seed(session_factory)
    async with session_factory() as s:
        new = await rotate_endpoint_secret(s, application_id=app_a, endpoint_id=ep_id)
    assert new.startswith("whsec_") and new != old
    async with session_factory() as s:
        ep = await s.get(Endpoint, ep_id)
    box = get_secret_box()
    assert box.open(ep.secret) == new and box.open(ep.previous_secret) == old
    # within the window, a delivery verifies under both secrets
    headers = signing.sign(
        "p",
        message_id="m",
        secret=box.open(ep.secret),
        previous_secret=box.open(ep.previous_secret),
    )
    assert signing.verify("p", headers, secret=new)
    assert signing.verify("p", headers, secret=old)


@pytest.mark.asyncio
async def test_old_secret_dies_after_window(session_factory):
    app_a, _, ep_id, old = await _seed(session_factory)
    async with session_factory() as s:
        new = await rotate_endpoint_secret(s, application_id=app_a, endpoint_id=ep_id)
        await s.execute(update(Endpoint).where(Endpoint.id == ep_id)
                        .values(previous_secret_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        await s.commit()
    # worker drops an expired previous_secret → only the new secret signs
    headers = signing.sign("p", message_id="m", secret=new, previous_secret=None)
    assert signing.verify("p", headers, secret=new)
    assert not signing.verify("p", headers, secret=old)


@pytest.mark.asyncio
async def test_rotation_is_tenant_scoped(session_factory):
    _, app_b, ep_id, old = await _seed(session_factory)
    async with session_factory() as s:
        assert await rotate_endpoint_secret(s, application_id=app_b, endpoint_id=ep_id) is None
    async with session_factory() as s:
        assert get_secret_box().open((await s.get(Endpoint, ep_id)).secret) == old   # untouched