"""per-endpoint concurrency cap

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-20 03:15:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT = "max_concurrent_positive"
_INFLIGHT_INDEX = "ix_deliveries_inflight"


def upgrade() -> None:
    """Add the optional per-endpoint cap and an index for the in-flight count."""
    op.add_column(
        "endpoints",
        sa.Column("max_concurrent_deliveries", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        op.f(_CONSTRAINT), "endpoints",
        "max_concurrent_deliveries IS NULL OR max_concurrent_deliveries > 0",
    )
    # Partial index over leased rows only — supports the per-endpoint in-flight
    # aggregate in claim_deliveries without scanning the whole table.
    op.create_index(
        _INFLIGHT_INDEX, "deliveries", ["endpoint_id"],
        postgresql_where=sa.text("locked_by IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INFLIGHT_INDEX, table_name="deliveries")
    op.drop_constraint(op.f(_CONSTRAINT), "endpoints", type_="check")
    op.drop_column("endpoints", "max_concurrent_deliveries")
