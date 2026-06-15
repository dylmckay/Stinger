"""Read-side queries for the dashboard, plus replay.

Lists use keyset (cursor) pagination on the UUIDv7 primary key, not OFFSET:
because v7 ids are time-ordered and Postgres compares uuids byte-wise,
`id < cursor ORDER BY id DESC` yields stable, newest-first pages in O(log n) -
pages don't shift when new rows are inserted, and deep pages don't
scan-and-discard. Every query is scoped to the caller's application; deliveries
and attempts are reached only through their event's application_id, so one
tenant can never read another's data.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest import NOTIFY_CHANNEL
from app.models import Delivery, DeliveryAttempt, Endpoint, EndpointEventType, Event, EventType

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True)
class Page:
    items: list
    next_cursor: str | None


@dataclass(frozen=True)
class DeliveryDetail:
    delivery: Delivery
    event: Event
    endpoint: Endpoint
    attempts: list[DeliveryAttempt]


def _clamp(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


async def list_deliveries(
    session: AsyncSession,
    *,
    application_id: uuid.UUID,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    status: str | None = None,
    endpoint_id: uuid.UUID | None = None
) -> Page:
    limit = _clamp(limit)
    q = (
        select(Delivery)
        .join(Event, Event.id == Delivery.event_id)
        .where(Event.application_id == application_id)
        .order_by(Delivery.id.desc())
        .limit(limit + 1)                       # one extra row = "is there more?"
    )
    if cursor:
        q = q.where(Delivery.id < uuid.UUID(cursor))
    if status:
        q = q.where(Delivery.status == status)
    if endpoint_id:
        q = q.where(Delivery.endpoint_id == endpoint_id)

    rows = list((await session.scalars(q)).all())
    # cursor is the last RETURNED row, never the peeked extra one
    next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
    return Page(items=rows[:limit], next_cursor=next_cursor)


async def get_delivery_detail(session: AsyncSession, *, application_id: uuid.UUID, delivery_id: uuid.UUID) -> DeliveryDetail | None:
    delivery = await session.scalar(
        select(Delivery)
        .join(Event, Event.id == Delivery.event_id)
        .where(Delivery.id == delivery_id, Event.application_id == application_id)
    )
    if delivery is None:                        # not found OR not this tenant's
        return None
    event = await session.get(Event, delivery.event_id)
    endpoint = await session.get(Endpoint, delivery.endpoint_id)
    attempts = list((await session.scalars(
        select(DeliveryAttempt)
        .where(DeliveryAttempt.delivery_id == delivery_id)
        .order_by(DeliveryAttempt.attempt_number)
    )).all())
    return DeliveryDetail(delivery=delivery, event=event, endpoint=endpoint, attempts=attempts)


async def list_events(session: AsyncSession, *, application_id: uuid.UUID, limit: int = DEFAULT_LIMIT, cursor: str | None = None) -> Page:
    limit = _clamp(limit)
    q = (
        select(Event)
        .where(Event.application_id == application_id)
        .order_by(Event.id.desc())
        .limit(limit + 1)
    )
    if cursor:
        q = q.where(Event.id < uuid.UUID(cursor))
    rows = list((await session.scalars(q)).all())
    next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
    return Page(items=rows[:limit], next_cursor=next_cursor)


async def list_endpoints(session: AsyncSession, *, application_id: uuid.UUID) -> list[tuple[Endpoint, list[str]]]:
    endpoints = list((await session.scalars(
        select(Endpoint)
        .where(Endpoint.application_id == application_id)
        .order_by(Endpoint.created_at.desc())
    )).all())
    if not endpoints:
        return []
    subs = (await session.execute(
        select(EndpointEventType.endpoint_id, EventType.name)
        .join(EventType, EventType.id == EndpointEventType.event_type_id)
        .where(EndpointEventType.endpoint_id.in_([e.id for e in endpoints]))
    )).all()
    by_endpoint: dict[uuid.UUID, list[str]] = {}
    for endpoint_id, type_name in subs:
        by_endpoint.setdefault(endpoint_id, []).append(type_name)
    return [(ep, by_endpoint.get(ep.id, [])) for ep in endpoints]


async def replay_delivery(session: AsyncSession, *, application_id: uuid.UUID, delivery_id: uuid.UUID) -> uuid.UUID | None:
    """Re-deliver an existing event to its endpoint by inserting a FRESH
    delivery row for the same (event, endpoint). The original's history is
    untouched; the replay gets its own attempt timeline.
    """
    original = await session.scalar(
        select(Delivery)
        .join(Event, Event.id == Delivery.event_id)
        .where(Delivery.id == delivery_id, Event.application_id == application_id)
    )
    if original is None:
        return None
    replay = Delivery(event_id=original.event_id, endpoint_id=original.endpoint_id)
    session.add(replay)
    await session.flush()                       # materialize replay.id
    await session.execute(select(func.pg_notify(NOTIFY_CHANNEL, "")))
    await session.commit()
    return replay.id