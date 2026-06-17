"""Stinger admin CLI — bootstrap applications, event types, endpoints, and keys.

These are CLI commands rather than API endpoints because they bootstrap the very
first application and key, before any credential exists to authenticate a
management API (the same chicken-and-egg that makes key creation a CLI command).
Run them inside the running container:

    docker compose exec api python -m app.cli create-application "Acme"
    docker compose exec api python -m app.cli add-event-type   <app_id> invoice.paid
    docker compose exec api python -m app.cli add-endpoint      <app_id> https://example.com/hook --event-type invoice.paid
    docker compose exec api python -m app.cli issue-key         <app_id> --name prod
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import create_api_key
from app.config import get_settings
from app.delivery import signing
from app.models import Application, Endpoint, EndpointEventType, EventType
from app.crypto import get_secret_box


class CLIError(Exception):
    """A user-facing error (bad id, missing/duplicate entity) — no traceback."""


def _parse_uuid(label: str, value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise CLIError(f"{label} is not a valid UUID: {value!r}")


async def _require_application(session, application_id: uuid.UUID) -> Application:
    application = await session.get(Application, application_id)
    if application is None:
        raise CLIError(f"application not found: {application_id}")
    return application


async def cmd_create_application(session, *, name: str) -> str:
    application = Application(name=name)
    session.add(application)
    await session.commit()
    return f"application created\n  id:   {application.id}\n  name: {application.name}"


async def cmd_add_event_type(session, *, application_id: uuid.UUID, name: str) -> str:
    await _require_application(session, application_id)
    event_type = EventType(application_id=application_id, name=name)
    session.add(event_type)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise CLIError(f"event type {name!r} already exists for this application")
    return f"event type created\n  id:   {event_type.id}\n  name: {event_type.name}"


async def cmd_add_endpoint(
    session, *, application_id: uuid.UUID, url: str, event_types: list[str]
) -> str:
    await _require_application(session, application_id)
    # Resolve every requested event type up front; fail before creating anything.
    resolved = []
    for name in event_types:
        event_type = await session.scalar(
            select(EventType).where(
                EventType.application_id == application_id, EventType.name == name
            )
        )
        if event_type is None:
            raise CLIError(
                f"event type {name!r} is not registered for this application — "
                f"add it first:  add-event-type {application_id} {name}"
            )
        resolved.append(event_type)

    secret = signing.generate_secret()
    endpoint = Endpoint(application_id=application_id, url=url, secret=get_secret_box().seal(secret))
    session.add(endpoint)
    await session.flush()
    for event_type in resolved:
        session.add(EndpointEventType(endpoint_id=endpoint.id, event_type_id=event_type.id))
    await session.commit()

    subscribed = ", ".join(e.name for e in resolved)
    return (
        f"endpoint created\n"
        f"  id:     {endpoint.id}\n"
        f"  url:    {endpoint.url}\n"
        f"  events: {subscribed}\n"
        f"  secret: {secret}\n"
        f"  (give this signing secret to the receiver — it verifies deliveries with it)"
    )


async def cmd_issue_key(session, *, application_id: uuid.UUID, name: str | None) -> str:
    await _require_application(session, application_id)
    full, row = await create_api_key(session, application_id=application_id, name=name)
    return f"API key issued (shown once — store it now)\n  id:  {row.id}\n  key: {full}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stinger", description="Stinger admin CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-application", help="create a new application (tenant)")
    p.add_argument("name")

    p = sub.add_parser("add-event-type", help="register an event type for an application")
    p.add_argument("application_id")
    p.add_argument("name")

    p = sub.add_parser("add-endpoint", help="add a receiving endpoint and subscribe it to event types")
    p.add_argument("application_id")
    p.add_argument("url")
    p.add_argument("--event-type", action="append", dest="event_types", metavar="NAME",
                   required=True, help="event type to subscribe to (repeatable)")

    p = sub.add_parser("issue-key", help="mint an API key for an application")
    p.add_argument("application_id")
    p.add_argument("--name", default=None, help="optional label for the key")

    return parser


async def _dispatch(args: argparse.Namespace) -> str:
    engine = create_async_engine(get_settings().DATABASE_URL)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            if args.command == "create-application":
                return await cmd_create_application(session, name=args.name)
            if args.command == "add-event-type":
                return await cmd_add_event_type(
                    session,
                    application_id=_parse_uuid("application_id", args.application_id),
                    name=args.name,
                )
            if args.command == "add-endpoint":
                return await cmd_add_endpoint(
                    session,
                    application_id=_parse_uuid("application_id", args.application_id),
                    url=args.url,
                    event_types=args.event_types,
                )
            if args.command == "issue-key":
                return await cmd_issue_key(
                    session,
                    application_id=_parse_uuid("application_id", args.application_id),
                    name=args.name,
                )
            raise CLIError(f"unknown command: {args.command}")
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        print(asyncio.run(_dispatch(args)))
        return 0
    except CLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
