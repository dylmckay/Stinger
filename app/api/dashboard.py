from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import reads
from app.api.deps import current_application, get_session
from app.models import Application

router = APIRouter(prefix="/api/v1", tags=["dashboard"])


class DeliveryOut(BaseModel):
    id: UUID
    status: str
    attempt_count: int
    next_attempt_at: datetime
    event_id: UUID
    endpoint_id: UUID
    created_at: datetime
    model_config = {"from_attributes": True}


class AttemptOut(BaseModel):
    attempt_number: int
    response_status: int | None
    response_body: str | None
    error: str | None
    latency_ms: int | None
    created_at: datetime
    model_config = {"from_attributes": True}


class EventOut(BaseModel):
    id: UUID
    payload: str
    idempotency_key: str | None
    created_at: datetime
    model_config = {"from_attributes": True}


class EndpointOut(BaseModel):
    id: UUID
    url: str
    status: str
    model_config = {"from_attributes": True}


class DeliveryListOut(BaseModel):
    items: list[DeliveryOut]
    next_cursor: str | None


class DeliveryDetailOut(BaseModel):
    delivery: DeliveryOut
    event: EventOut
    endpoint: EndpointOut
    attempts: list[AttemptOut]


@router.get("/deliveries", response_model=DeliveryListOut)
async def get_deliveries(
    application: Application = Depends(current_application),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    status: str | None = None,
):
    page = await reads.list_deliveries(
        session, application_id=application.id, limit=limit, cursor=cursor, status=status
    )
    return DeliveryListOut(items=page.items, next_cursor=page.next_cursor)


@router.get("/deliveries/{delivery_id}", response_model=DeliveryDetailOut)
async def get_delivery(
    delivery_id: UUID,
    application: Application = Depends(current_application),
    session: AsyncSession = Depends(get_session),
):
    detail = await reads.get_delivery_detail(
        session, application_id=application.id, delivery_id=delivery_id
    )
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "delivery not found")
    return detail   # field names line up; from_attributes handles the ORM rows


@router.post("/deliveries/{delivery_id}/replay", status_code=status.HTTP_202_ACCEPTED)
async def replay(
    delivery_id: UUID,
    application: Application = Depends(current_application),
    session: AsyncSession = Depends(get_session),
):
    new_id = await reads.replay_delivery(
        session, application_id=application.id, delivery_id=delivery_id
    )
    if new_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "delivery not found")
    return {"delivery_id": new_id}