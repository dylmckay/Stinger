"""Stinger ORM models — schema decisions documented in docs/ARCHITECTURE.md."""
import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, MetaData, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _uuid7() -> uuid.UUID:
    return uuid.uuid7()


class EndpointStatus(enum.StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class DeliveryStatus(enum.StrEnum):
    PENDING = "pending"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    EXHAUSTED = "exhausted"
    DISCARDED = "discarded"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)


# ------------Tables------------


class Application(TimestampMixin, Base):
    __tablename__ = "applications"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=_uuid7)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class EventType(TimestampMixin, Base):
    __tablename__ = "event_types"
    __table_args__ = (UniqueConstraint("application_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=_uuid7)
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. "invoice.paid"
    description: Mapped[str | None] = mapped_column(Text)


class Endpoint(TimestampMixin, Base):
    __tablename__ = "endpoints"
    __table_args__ = (
        CheckConstraint(
            f"status IN ('{EndpointStatus.ENABLED}', '{EndpointStatus.DISABLED}')",
            name="status_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=_uuid7)
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    secret: Mapped[str] = mapped_column(Text, nullable=False)        # active signing secret
    previous_secret: Mapped[str | None] = mapped_column(Text)        # dual-sign window
    previous_secret_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, default=EndpointStatus.ENABLED)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    disabled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class EndpointEventType(Base):
    __tablename__ = "endpoint_event_types"

    endpoint_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("endpoints.id", ondelete="CASCADE"), primary_key=True)
    event_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("event_types.id", ondelete="CASCADE"), primary_key=True)


class Event(TimestampMixin, Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("application_id", "idempotency_key", name="uq_events_application_id_idempotency_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=_uuid7)
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), nullable=False)
    event_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("event_types.id"), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # verbatim bytes, decision 2
    idempotency_key: Mapped[str | None] = mapped_column(Text)


class Delivery(TimestampMixin, Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'retrying', 'succeeded', 'exhausted', 'discarded')",
            name="status_valid",
        ),
        Index(
            "ix_deliveries_claim",
            "next_attempt_at",
            postgresql_where="status IN ('pending', 'retrying')",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=_uuid7)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default=DeliveryStatus.PENDING)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    locked_by: Mapped[str | None] = mapped_column(Text, nullable=True)


class DeliveryAttempt(TimestampMixin, Base):
    __tablename__ = "delivery_attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=_uuid7)
    delivery_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deliveries.id", ondelete="CASCADE"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)  # None = no HTTP response
    response_body: Mapped[str | None] = mapped_column(Text)       # truncated at write time
    error: Mapped[str | None] = mapped_column(Text)               # timeout, DNS, SSRF block...
    latency_ms: Mapped[int | None] = mapped_column(Integer)