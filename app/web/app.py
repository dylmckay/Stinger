"""Build the dashboard FastAPI app (the `api` image serves this alongside the
JSON API). Server-rendered Jinja + HTMX: no Node, no build step, no CORS.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse, Response

from app.web import auth, views
from app.web.deps import AuthRedirect

_HERE = Path(__file__).parent


def _humanize_age(dt: datetime) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 0:                                    # future (e.g. next_attempt_at)
        secs = -secs
        if secs < 60:
            return f"in {int(secs)}s"
        if secs < 3600:
            return f"in {int(secs // 60)}m"
        return f"in {int(secs // 3600)}h"
    if secs < 60:
        return f"{int(secs)}s ago"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def create_web_app(session_factory: async_sessionmaker[AsyncSession], *, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=secret_key, https_only=False)
    app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parents[1] / "static"), name="static")

    templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "static" / "templates")
    templates.env.filters["age"] = _humanize_age
    from app.web.deps import csrf_token as _csrf_token
    templates.env.globals["csrf_token"] = _csrf_token
    app.state.templates = templates
    app.state.session_factory = session_factory

    app.include_router(auth.router)
    app.include_router(views.router)

    @app.exception_handler(AuthRedirect)
    async def _auth_redirect(request: Request, exc: AuthRedirect) -> Response:
        # HTMX requests can't follow a body redirect cleanly; use HX-Redirect.
        if request.headers.get("HX-Request") == "true":
            return Response(status_code=204, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    @app.get("/")
    async def root() -> Response:
        return RedirectResponse("/dashboard/deliveries", status_code=307)

    return app
