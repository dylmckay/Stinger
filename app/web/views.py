"""Dashboard views over the read side: deliveries list, delivery detail
(the attempt-timeline centerpiece), and the replay action.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app import reads
from app.models import Application, Endpoint, Event, EventType
from app.web.deps import current_application_web, get_session, is_htmx

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

PAGE_SIZE = 25
TERMINAL = {"succeeded", "exhausted", "discarded"}
KNOWN_STATUSES = ("pending", "retrying", "succeeded", "exhausted", "discarded")


async def _enrich(session: AsyncSession, deliveries) -> list[dict]:
    """Attach display fields (event type, endpoint url) to delivery rows."""
    if not deliveries:
        return []
    ep_ids = {d.endpoint_id for d in deliveries}
    ev_ids = {d.event_id for d in deliveries}
    eps = {e.id: e for e in (await session.scalars(
        select(Endpoint).where(Endpoint.id.in_(ep_ids)))).all()}
    evs = {e.id: e for e in (await session.scalars(
        select(Event).where(Event.id.in_(ev_ids)))).all()}
    et_ids = {e.event_type_id for e in evs.values()}
    ets = {t.id: t.name for t in (await session.scalars(
        select(EventType).where(EventType.id.in_(et_ids)))).all()} if et_ids else {}
    rows = []
    for d in deliveries:
        ev = evs.get(d.event_id)
        ep = eps.get(d.endpoint_id)
        rows.append({
            "id": d.id,
            "status": d.status,
            "attempt_count": d.attempt_count,
            "created_at": d.created_at,
            "endpoint_url": ep.url if ep else "—",
            "event_type": ets.get(ev.event_type_id) if ev else "—",
        })
    return rows


@router.get("")
async def home():
    return Response(status_code=307, headers={"Location": "/dashboard/deliveries"})


@router.get("/deliveries")
async def deliveries(
    request: Request,
    status: str | None = None,
    cursor: str | None = None,
    application: Application = Depends(current_application_web),
    session: AsyncSession = Depends(get_session),
):
    status = status if status in KNOWN_STATUSES else None
    page = await reads.list_deliveries(
        session, application_id=application.id,
        limit=PAGE_SIZE, cursor=cursor, status=status,
    )
    rows = await _enrich(session, page.items)
    ctx = {
        "rows": rows,
        "next_cursor": page.next_cursor,
        "status": status,
        "statuses": KNOWN_STATUSES,
    }
    templates = request.app.state.templates
    if is_htmx(request):
        if cursor:                                  # "load more" appends rows only
            return templates.TemplateResponse(request, "_delivery_rows.html", ctx)
        return templates.TemplateResponse(request, "_delivery_table.html", ctx)  # filter swap
    return templates.TemplateResponse(request, "deliveries.html", ctx)


@router.get("/deliveries/{delivery_id}")
async def delivery_detail(
    request: Request,
    delivery_id: UUID,
    application: Application = Depends(current_application_web),
    session: AsyncSession = Depends(get_session),
):
    detail = await reads.get_delivery_detail(
        session, application_id=application.id, delivery_id=delivery_id
    )
    templates = request.app.state.templates
    if detail is None:
        return templates.TemplateResponse(
            request, "not_found.html", {}, status_code=404
        )
    return templates.TemplateResponse(
        request, "delivery_detail.html",
        {"d": detail, "terminal": detail.delivery.status in TERMINAL},
    )


@router.get("/deliveries/{delivery_id}/timeline")
async def delivery_timeline(
    request: Request,
    delivery_id: UUID,
    application: Application = Depends(current_application_web),
    session: AsyncSession = Depends(get_session),
):
    """Polled fragment: re-renders the status + attempt timeline in place."""
    detail = await reads.get_delivery_detail(
        session, application_id=application.id, delivery_id=delivery_id
    )
    if detail is None:
        return Response(status_code=404)
    return request.app.state.templates.TemplateResponse(
        request, "_timeline.html",
        {"d": detail, "terminal": detail.delivery.status in TERMINAL},
    )


@router.post("/deliveries/{delivery_id}/replay")
async def replay(
    request: Request,
    delivery_id: UUID,
    application: Application = Depends(current_application_web),
    session: AsyncSession = Depends(get_session),
):
    new_id = await reads.replay_delivery(
        session, application_id=application.id, delivery_id=delivery_id
    )
    if new_id is None:
        return Response(status_code=404)
    # Land the operator on the fresh delivery so they watch the new attempt.
    return Response(status_code=204, headers={"HX-Redirect": f"/dashboard/deliveries/{new_id}"})
