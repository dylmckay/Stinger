"""Composition root for the `api` process.

One FastAPI app serves both surfaces: the JSON API at `/api/v1/*`, and the
server-rendered dashboard mounted at `/` (which brings its own SessionMiddleware,
static files, and templates). Explicit API routes are registered before the
mount, so they win; everything else (`/`, `/login`, `/dashboard/*`, `/static/*`)
falls through to the dashboard app.

Run with:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.responses import JSONResponse

from app.api import dashboard as api_dashboard
from app.api import deps as api_deps
from app.api import events as api_events
from app.config import get_settings
from app.web.app import create_web_app

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stinger.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await app.state.engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=10, max_overflow=5)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    app = FastAPI(title="Stinger", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.session_factory = session_factory

    # The JSON API's get_session is a stub by design (overridden in tests);
    # wire it here to the real factory. (Alternatively, change api.deps.get_session
    # to read request.app.state.session_factory and drop this override.)
    async def get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[api_deps.get_session] = get_session
    app.include_router(api_events.router)
    app.include_router(api_dashboard.router)

    @app.get("/healthz")
    async def healthz():
        return {"status": "healthy"}

    @app.exception_handler(Exception)
    async def on_unhandled(request: Request, exc: Exception):
        # Log the detail server-side; never return internals to the caller.
        log.exception("unhandled error: %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    # Dashboard mounted last so the explicit API routes above take precedence.
    app.mount("/", create_web_app(session_factory, secret_key=settings.SECRET_KEY))
    return app


app = create_app()
