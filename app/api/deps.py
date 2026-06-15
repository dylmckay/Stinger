from collections.abc import AsyncIterator

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import Application

_engine = create_async_engine(get_settings().DATABASE_URL, pool_size=10, max_overflow=5)
_Session = async_sessionmaker(_engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    # publish_event commits its own transaction; this dependency only scopes
    # the session lifetime, it does not commit.
    async with _Session() as session:
        yield session


async def current_application(
    x_api_key: str = Header(...),
    session: AsyncSession = Depends(get_session),
) -> Application:
    # SEAM: real auth hashes the presented key, looks it up, maps to an app.
    # API-key management (generation, hashing, rotation) is its own piece.
    raise NotImplementedError("API-key auth pending")