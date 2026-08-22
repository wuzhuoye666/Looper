"""persist deterministic attempt queue order

Revision ID: b7e91d42c5fa
Revises: a4c8e2f91b73
Create Date: 2026-08-21 12:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e91d42c5fa"
down_revision: str | Sequence[str] | None = "a4c8e2f91b73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.drop_index("ix_attempt_claim")
        batch_op.add_column(
            sa.Column("queue_sequence", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.create_index(
            "ix_attempt_claim",
            ["status", "queue_sequence", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.drop_index("ix_attempt_claim")
        batch_op.drop_column("queue_sequence")
        batch_op.create_index("ix_attempt_claim", ["status", "created_at"], unique=False)
