"""encrypt signing secrets at rest

Seals existing plaintext `whsec_…` values in endpoints.secret / previous_secret
under envelope encryption. No schema change: the token packs into the existing
Text column. Idempotent — already-sealed rows (stcr.…) are skipped — so a re-run
or a fresh install with zero rows is a harmless no-op.

OPERATIONAL: the encryption key must be present in the environment when this
runs (STINGER_ENCRYPTION_KEY, or SECRET_KEY as the documented fallback). The
docker-compose `migrate` service already receives SECRET_KEY; add
STINGER_ENCRYPTION_KEY there too if you set one.

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.crypto import get_secret_box

revision = "0003"
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLS = ("secret", "previous_secret")


def upgrade() -> None:
    box = get_secret_box()
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, secret, previous_secret FROM endpoints")
    ).mappings().all()

    for row in rows:
        updates = {
            col: box.seal(row[col])
            for col in _COLS
            if row[col] is not None and not box.is_sealed(row[col])
        }
        if updates:
            sets = ", ".join(f"{c} = :{c}" for c in updates)
            conn.execute(
                sa.text(f"UPDATE endpoints SET {sets} WHERE id = :id"),
                {**updates, "id": row["id"]},
            )


def downgrade() -> None:
    # Reverses the encryption — reintroduces plaintext-at-rest. Provided for
    # symmetry; only meaningful if you're rolling the feature back.
    box = get_secret_box()
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, secret, previous_secret FROM endpoints")
    ).mappings().all()

    for row in rows:
        updates = {
            col: box.open(row[col])
            for col in _COLS
            if row[col] is not None and box.is_sealed(row[col])
        }
        if updates:
            sets = ", ".join(f"{c} = :{c}" for c in updates)
            conn.execute(
                sa.text(f"UPDATE endpoints SET {sets} WHERE id = :id"),
                {**updates, "id": row["id"]},
            )