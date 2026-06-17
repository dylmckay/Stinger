"""Management JSON API: create and list event types and endpoints.

The CLI is still how the *first* application and key are bootstrapped (no
credential exists yet to authenticate these), but once a key exists everything
else can be driven here — so a user never has to touch the CLI again. Shares
`app.management` with the CLI and the dashboard forms.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import management, reads
from app.api.deps import current_application, get_session
from app.models import Application

router = APIRouter(prefix="/api/v1", tags=["management"])


# ---- event types ----

class EventTypeIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class EventTypeOut(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    model_config = {"from_attributes": True}


class EventTypeListOut(BaseModel):
    items: list[EventTypeOut]


@router.post("/event-types", response_model=EventTypeOut, status_code=status.HTTP_201_CREATED)
async def create_event_type(
    body: EventTypeIn,
    application: Application = Depends(current_application),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await management.create_event_type(
            session, application_id=application.id, name=body.name
        )
    except management.DuplicateEventType as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    except management.InvalidEventTypeName as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))


@router.get("/event-types", response_model=EventTypeListOut)
async def list_event_types(
    application: Application = Depends(current_application),
    session: AsyncSession = Depends(get_session),
):
    items = await reads.list_event_types(session, application_id=application.id)
    return EventTypeListOut(items=items)


# ---- endpoints ----

class EndpointIn(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    event_types: list[str] = Field(min_length=1)


class EndpointOut(BaseModel):
    id: UUID
    url: str
    status: str
    event_types: list[str]


class EndpointCreatedOut(EndpointOut):
    secret: str    # shown ONCE; only the sealed form is persisted


class EndpointListOut(BaseModel):
    items: list[EndpointOut]


@router.post("/endpoints", response_model=EndpointCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    body: EndpointIn,
    application: Application = Depends(current_application),
    session: AsyncSession = Depends(get_session),
):
    try:
        endpoint, secret = await management.create_endpoint(
            session,
            application_id=application.id,
            url=body.url,
            event_type_names=body.event_types,
        )
    except management.UnknownEventTypes as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"message": str(e), "unknown_event_types": e.names},
        )
    except management.InvalidEndpointURL as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))
    return EndpointCreatedOut(
        id=endpoint.id, url=endpoint.url, status=endpoint.status,
        event_types=body.event_types, secret=secret,
    )


@router.get("/endpoints", response_model=EndpointListOut)
async def list_endpoints(
    application: Application = Depends(current_application),
    session: AsyncSession = Depends(get_session),
):
    pairs = await reads.list_endpoints(session, application_id=application.id)
    return EndpointListOut(
        items=[
            EndpointOut(id=ep.id, url=ep.url, status=ep.status, event_types=types)
            for ep, types in pairs
        ]
    )