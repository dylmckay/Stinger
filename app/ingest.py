"""Event ingestion: persist an event, fan out to deliveries, wake workers.

Everything happens in ONE transaction, so there is never a window where an
event is stored but its deliveries are lost — the reason Postgres is the queue.
The pg_notify fires on commit, so a worker is nudged only once the new delivery
rows are actually visible.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Delivery, Endpoint, EndpointEventType, EndpointStatus, Event, EventType

NOTIFY_CHANNEL = "stinger_deliveries"
_EVENT_IDEMPOTENCY_CONSTRAINT = "uq_events_application_id_idempotency_key"


class UnknownEventType(Exception):
    """The named event type isn't registered for this application."""


@dataclass(frozen=True)
class PublishResult:
    event_id: uuid.UUID
    delivery_count: int
    idempotent_replay: bool


def _canonical(payload: Any) -> str:
    # Normalize once, at the boundary: compact, key-order preserved. Stored,
    # delivered, and signed bytes are then identical forever after.
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


async def publish_event(session: AsyncSession, *, application_id: uuid.UUID, event_type_name: str, payload: Any, idempotency_key: str | None = None) -> PublishResult:
    event_type = await session.scalar(
        select(EventType).where(
            EventType.application_id == application_id,
            EventType.name == event_type_name,
        )
    )
    if event_type is None:
        raise UnknownEventType(event_type_name)

    # Idempotent insert: a duplicate (app, key) returns nothing -> replay.
    insert_event = (
        pg_insert(Event)
        .values(
            id=uuid.uuid7(),
            application_id=application_id,
            event_type_id=event_type.id,
            payload=_canonical(payload),
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing(constraint=_EVENT_IDEMPOTENCY_CONSTRAINT)
        .returning(Event.id)
    )
    inserted_id = await session.scalar(insert_event)

    if inserted_id is None:
        # Replay: the event already exists; do NOT re-fan-out.
        existing_id = await session.scalar(
            select(Event.id).where(
                Event.application_id == application_id,
                Event.idempotency_key == idempotency_key,
            )
        )
        count = await session.scalar(
            select(func.count()).select_from(Delivery).where(Delivery.event_id == existing_id)
        )
        await session.commit()
        return PublishResult(event_id=existing_id, delivery_count=count, idempotent_replay=True)

    # Fan out to every ENABLED endpoint subscribed to this event type.
    endpoint_ids = (await session.scalars(
        select(EndpointEventType.endpoint_id)
        .join(Endpoint, Endpoint.id == EndpointEventType.endpoint_id)
        .where(
            EndpointEventType.event_type_id == event_type.id,
            Endpoint.application_id == application_id,
            Endpoint.status == EndpointStatus.ENABLED,
        )
    )).all()

    for endpoint_id in endpoint_ids:
        session.add(Delivery(event_id=inserted_id, endpoint_id=endpoint_id))

    if endpoint_ids:
        # Fires on COMMIT, after the delivery rows are visible.
        await session.execute(select(func.pg_notify(NOTIFY_CHANNEL, "")))

    await session.commit()
    return PublishResult(
        event_id=inserted_id, delivery_count=len(endpoint_ids), idempotent_replay=False,
    )