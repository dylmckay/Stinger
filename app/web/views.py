"""Dashboard views over the read side: deliveries list, delivery detail
(the attempt-timeline centerpiece), and the replay action.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app import reads
from app.delivery.record import reenable_endpoint, rotate_endpoint_secret
from app.models import Application, Delivery, Endpoint, EndpointEventType, Event, EventType
from app.web.deps import current_application_web, get_session, is_htmx
from app import management

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


# ---- endpoints page (create + list) ----

async def _endpoints_content_ctx(
    session, application_id, *,
    revealed_id=None, revealed_secret=None,
    error=None, form_url="", selected=(),
    et_error=None, et_name="",
) -> dict:
    """Context for _ep_content.html. On a successful create, pass the new
    endpoint's id + secret so that one card renders its reveal block (once)."""
    pairs = await reads.list_endpoints(session, application_id=application_id)
    cards = [
        _card_ctx(ep, types, revealed_secret if ep.id == revealed_id else None)
        for ep, types in pairs
    ]
    event_types = await reads.list_event_types(session, application_id=application_id)
    return {
        "cards": cards,
        "event_types": event_types,
        "error": error, "form_url": form_url, "selected": list(selected),
        "et_error": et_error, "et_name": et_name,
    }

def _card_ctx(ep: Endpoint, types: list[str], revealed_secret: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    expires = ep.previous_secret_expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return {
        "ep": ep,
        "types": types,
        "rotating": bool(expires and expires > now),
        "revealed_secret": revealed_secret,
    }


async def _load_card(
    session: AsyncSession, application_id, endpoint_id, revealed_secret: str | None = None
) -> dict | None:
    ep = await session.scalar(
        select(Endpoint).where(
            Endpoint.id == endpoint_id, Endpoint.application_id == application_id
        )
    )
    if ep is None:
        return None
    types = [
        name for (_eid, name) in (await session.execute(
            select(EndpointEventType.endpoint_id, EventType.name)
            .join(EventType, EventType.id == EndpointEventType.event_type_id)
            .where(EndpointEventType.endpoint_id == endpoint_id)
        )).all()
    ]
    return _card_ctx(ep, types, revealed_secret)


@router.get("/endpoints")
async def endpoints(
    request: Request,
    application: Application = Depends(current_application_web),
    session: AsyncSession = Depends(get_session),
):
    ctx = await _endpoints_content_ctx(session, application.id)
    return request.app.state.templates.TemplateResponse(request, "endpoints.html", ctx)


@router.post("/endpoints/{endpoint_id}/reenable")
async def reenable_ep(
    request: Request,
    endpoint_id: UUID,
    application: Application = Depends(current_application_web),
    session: AsyncSession = Depends(get_session),
):
    ok = await reenable_endpoint(
        session, application_id=application.id, endpoint_id=endpoint_id
    )
    if not ok:
        return Response(status_code=404)
    card = await _load_card(session, application.id, endpoint_id)
    return request.app.state.templates.TemplateResponse(
        request, "_endpoint_card.html", {"card": card}
    )


@router.post("/endpoints/{endpoint_id}/rotate-secret")
async def rotate_ep(
    request: Request,
    endpoint_id: UUID,
    application: Application = Depends(current_application_web),
    session: AsyncSession = Depends(get_session),
):
    new_secret = await rotate_endpoint_secret(
        session, application_id=application.id, endpoint_id=endpoint_id
    )
    if new_secret is None:
        return Response(status_code=404)
    card = await _load_card(session, application.id, endpoint_id, revealed_secret=new_secret)
    return request.app.state.templates.TemplateResponse(
        request, "_endpoint_card.html", {"card": card}
    )


@router.post("/endpoints")
async def create_endpoint_web(
    request: Request,
    url: str = Form(""),
    event_types: list[str] | None = Form(None),
    application: Application = Depends(current_application_web),
    session: AsyncSession = Depends(get_session),
):
    event_types = event_types or []
    templates = request.app.state.templates
    if not event_types:
        ctx = await _endpoints_content_ctx(
            session, application.id,
            error="Select at least one event type.", form_url=url, selected=event_types,
        )
        return templates.TemplateResponse(request, "_ep_content.html", ctx)
    try:
        endpoint, secret = await management.create_endpoint(
            session, application_id=application.id, url=url, event_type_names=event_types,
        )
    except management.ManagementError as e:
        ctx = await _endpoints_content_ctx(
            session, application.id, error=str(e), form_url=url, selected=event_types,
        )
        return templates.TemplateResponse(request, "_ep_content.html", ctx)
    ctx = await _endpoints_content_ctx(
        session, application.id, revealed_id=endpoint.id, revealed_secret=secret,
    )
    return templates.TemplateResponse(request, "_ep_content.html", ctx)
 
 
@router.post("/endpoints/event-types")
async def quick_add_event_type_web(
    request: Request,
    name: str = Form(""),
    application: Application = Depends(current_application_web),
    session: AsyncSession = Depends(get_session),
):
    """Quick-add an event type from the endpoints page; re-renders the region so
    the new type immediately appears as a checkbox in the create form."""
    et_error = None
    try:
        await management.create_event_type(session, application_id=application.id, name=name)
        name = ""
    except management.ManagementError as e:
        et_error = str(e)
    ctx = await _endpoints_content_ctx(
        session, application.id, et_error=et_error, et_name=("" if not et_error else name),
    )
    return request.app.state.templates.TemplateResponse(request, "_ep_content.html", ctx)
 
 
# ---- event types page (full management surface) ----
 
@router.get("/event-types")
async def event_types_page(
    request: Request,
    application: Application = Depends(current_application_web),
    session: AsyncSession = Depends(get_session),
):
    types = await reads.list_event_types(session, application_id=application.id)
    return request.app.state.templates.TemplateResponse(
        request, "event_types.html", {"event_types": types}
    )
 
 
@router.post("/event-types")
async def create_event_type_web(
    request: Request,
    name: str = Form(""),
    application: Application = Depends(current_application_web),
    session: AsyncSession = Depends(get_session),
):
    error = None
    try:
        await management.create_event_type(session, application_id=application.id, name=name)
    except management.ManagementError as e:
        error = str(e)
    types = await reads.list_event_types(session, application_id=application.id)
    return request.app.state.templates.TemplateResponse(
        request, "_event_type_panel.html",
        {"event_types": types, "error": error, "name": ("" if not error else name)},
    )
# ---- events log ----

async def _event_rows(session: AsyncSession, events) -> list[dict]:
    """Attach event-type name and fan-out (delivery) count to event rows."""
    if not events:
        return []
    ev_ids = [e.id for e in events]
    counts = dict((await session.execute(
        select(Delivery.event_id, func.count())
        .where(Delivery.event_id.in_(ev_ids))
        .group_by(Delivery.event_id)
    )).all())
    et_ids = {e.event_type_id for e in events}
    ets = {t.id: t.name for t in (await session.scalars(
        select(EventType).where(EventType.id.in_(et_ids)))).all()}
    return [
        {
            "id": e.id,
            "event_type": ets.get(e.event_type_id, "—"),
            "payload": e.payload,
            "idempotency_key": e.idempotency_key,
            "delivery_count": counts.get(e.id, 0),
            "created_at": e.created_at,
        }
        for e in events
    ]


@router.get("/events")
async def events(
    request: Request,
    cursor: str | None = None,
    application: Application = Depends(current_application_web),
    session: AsyncSession = Depends(get_session),
):
    page = await reads.list_events(
        session, application_id=application.id, limit=PAGE_SIZE, cursor=cursor
    )
    ctx = {"rows": await _event_rows(session, page.items), "next_cursor": page.next_cursor}
    templates = request.app.state.templates
    if is_htmx(request) and cursor:                 # load more appends rows only
        return templates.TemplateResponse(request, "_event_rows.html", ctx)
    return templates.TemplateResponse(request, "events.html", ctx)
