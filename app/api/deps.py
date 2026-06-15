from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import authenticate
from app.models import Application


_bearer = HTTPBearer(auto_error=False)


async def get_session() -> AsyncIterator[AsyncSession]: # overridden at app startup
    raise NotImplementedError("wire get_session in app startup")


async def current_application(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> Application:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    application = await authenticate(session, creds.credentials)
    if application is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or revoked APi key", headers={"WWW-Authenticate": "Bearer"})
    return application
