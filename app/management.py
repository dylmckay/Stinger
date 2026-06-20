"""Management-plane writes: create event types and endpoints, with the
validation that guards them.

This is the single implementation behind all three entry points — the JSON
management API, the dashboard forms, and the admin CLI — so the creation rules
(event-type resolution, secret generation + sealing, subscription wiring) live
in exactly one place rather than being re-derived per surface. Reads
(`list_endpoints`, `list_event_types`) stay in `app.reads`; this module owns the
writes.

On URL validation: we check *format* only (scheme + host), not reachability.
The worker's SSRF guard resolves and pins the address at delivery time, and that
is the real security boundary — resolving at creation would be flaky (DNS,
transient outages) and give false confidence that a now-valid host stays valid.
"""
from __future__ import annotations

import uuid
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import get_secret_box
from app.delivery import signing
from app.models import Application, Endpoint, EndpointEventType, EventType


class ManagementError(Exception):
    """Base for user-facing management errors — surfaces map this to HTTP
    status / CLI message / form error at each entry point."""


class ApplicationNotFound(ManagementError):
    def __init__(self, application_id: uuid.UUID) -> None:
        super().__init__(f"application not found: {application_id}")


class DuplicateEventType(ManagementError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"event type {name!r} already exists for this application")


class UnknownEventTypes(ManagementError):
    def __init__(self, names: list[str]) -> None:
        self.names = names
        joined = ", ".join(repr(n) for n in names)
        super().__init__(f"event type(s) not registered for this application: {joined}")


class InvalidEndpointURL(ManagementError):
    pass


class InvalidEndpointConfig(ManagementError):
    pass


class InvalidEventTypeName(ManagementError):
    pass


_MAX_NAME_LEN = 255
_MAX_URL_LEN = 2048


def validate_event_type_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise InvalidEventTypeName("event type name cannot be empty")
    if len(name) > _MAX_NAME_LEN:
        raise InvalidEventTypeName(f"event type name exceeds {_MAX_NAME_LEN} characters")
    return name


def validate_endpoint_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise InvalidEndpointURL("endpoint URL cannot be empty")
    if len(url) > _MAX_URL_LEN:
        raise InvalidEndpointURL(f"endpoint URL exceeds {_MAX_URL_LEN} characters")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise InvalidEndpointURL("endpoint URL must use http:// or https://")
    if not parsed.netloc:
        raise InvalidEndpointURL("endpoint URL must include a host")
    return url


def validate_max_concurrent(value: int | None) -> int | None:
    """None means 'use the global default'; any explicit value must be positive
    (mirrors the max_concurrent_positive CHECK constraint)."""
    if value is None:
        return None
    if value < 1:
        raise InvalidEndpointConfig("max concurrent deliveries must be at least 1")
    return value


async def _require_application(session: AsyncSession, application_id: uuid.UUID) -> Application:
    application = await session.get(Application, application_id)
    if application is None:
        raise ApplicationNotFound(application_id)
    return application


async def create_event_type(
    session: AsyncSession, *, application_id: uuid.UUID, name: str
) -> EventType:
    await _require_application(session, application_id)
    name = validate_event_type_name(name)
    event_type = EventType(application_id=application_id, name=name)
    session.add(event_type)
    try:
        await session.commit()
    except IntegrityError:          # hits the (application_id, name) unique constraint
        await session.rollback()
        raise DuplicateEventType(name)
    return event_type


async def create_endpoint(
    session: AsyncSession,
    *,
    application_id: uuid.UUID,
    url: str,
    event_type_names: list[str],
    max_concurrent: int | None = None,
) -> tuple[Endpoint, str]:
    """Create an endpoint, subscribe it to the named event types, and return
    (endpoint, plaintext_secret). The plaintext secret is shown ONCE — only its
    sealed form is persisted. Raises before creating anything if any event type
    is unknown for this application.

    `max_concurrent` caps how many deliveries this endpoint may have in flight at
    once; None means use the worker's global default.
    """
    await _require_application(session, application_id)
    url = validate_endpoint_url(url)
    max_concurrent = validate_max_concurrent(max_concurrent)

    # Resolve every requested type up front (tenant-scoped), so a typo fails
    # cleanly before we create the endpoint or its subscriptions.
    resolved, missing = [], []
    for name in event_type_names:
        event_type = await session.scalar(
            select(EventType).where(
                EventType.application_id == application_id, EventType.name == name
            )
        )
        if event_type is None:
            missing.append(name)
        else:
            resolved.append(event_type)
    if missing:
        raise UnknownEventTypes(missing)

    plaintext = signing.generate_secret()
    endpoint = Endpoint(
        application_id=application_id,
        url=url,
        secret=get_secret_box().seal(plaintext),   # sealed at rest; plaintext returned once
        max_concurrent_deliveries=max_concurrent,
    )
    session.add(endpoint)
    await session.flush()
    for event_type in resolved:
        session.add(EndpointEventType(endpoint_id=endpoint.id, event_type_id=event_type.id))
    await session.commit()
    return endpoint, plaintext