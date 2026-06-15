from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_application, get_session
from app.ingest import UnknownEventType, publish_event
from app.models import Application

router = APIRouter(prefix="/api/v1", tags=["events"])


class PublishRequest(BaseModel):
    event_type: str
    payload: Any
    idempotency_key: str | None = None


class PublishResponse(BaseModel):
    event_id: UUID
    delivery_count: int
    idempotent_replay: bool


@router.post("/events", response_model=PublishResponse, status_code=status.HTTP_201_CREATED)
async def publish(
    body: PublishRequest,
    response: Response,
    application: Application = Depends(current_application),
    session: AsyncSession = Depends(get_session),
):
    try:
        result = await publish_event(
            session,
            application_id=application.id,
            event_type_name=body.event_type,
            payload=body.payload,
            idempotency_key=body.idempotency_key,
        )
    except UnknownEventType:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown event type: {body.event_type!r}",
        )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK   # 200 replay vs 201 created
    return result