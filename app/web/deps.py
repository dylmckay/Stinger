"""Dashboard web-layer dependencies.

Browser auth has no user/password table — it reuses the API-key model. On
login we validate a pasted key through the same `authenticate()` the JSON API
uses, then store the resolved application_id in a signed session cookie
(Starlette SessionMiddleware, signed with SECRET_KEY). Dashboard routes resolve
the application from that cookie, so tenant isolation is identical to the
Bearer-token path.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Application


class AuthRedirect(Exception):
    """Raised when a dashboard route is hit without a valid session."""


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


async def current_application_web(request: Request, session: AsyncSession = Depends(get_session)) -> Application:
    raw = request.session.get("application_id")
    if not raw:
        raise AuthRedirect()
    application = await session.scalar(
        select(Application).where(Application.id == uuid.UUID(raw))
    )
    if application is None:                 # key revoked / app deleted since login
        request.session.clear()
        raise AuthRedirect()
    return application


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"
