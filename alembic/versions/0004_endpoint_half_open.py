"""endpoint half-open breaker status

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-17 16:40:00.000000
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT = "status_valid"


def upgrade() -> None:
    """Allow the new 'half_open' endpoint status."""
    op.drop_constraint(_CONSTRAINT, "endpoints", type_="check")
    op.create_check_constraint(
        op.f(_CONSTRAINT), "endpoints",
        "status IN ('enabled', 'disabled', 'half_open')",
    )


def downgrade() -> None:
    """Revert to the two-state constraint.

    Any endpoint mid-recovery is collapsed back to 'disabled' first, so the
    narrower constraint can be re-applied without violation.
    """
    op.execute("UPDATE endpoints SET status = 'disabled' WHERE status = 'half_open'")
    op.drop_constraint(_CONSTRAINT, "endpoints", type_="check")
    op.create_check_constraint(
        op.f(_CONSTRAINT), "endpoints",
        "status IN ('enabled', 'disabled')",
    )