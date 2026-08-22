"""add target inventory lifecycle

Revision ID: f4a7c9d12e63
Revises: e6b91f4c2a70
Create Date: 2026-08-22 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4a7c9d12e63"
down_revision: str | Sequence[str] | None = "e6b91f4c2a70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("targets", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "lifecycle_status",
                sa.String(length=24),
                nullable=False,
                server_default="active",
            )
        )
        batch_op.add_column(sa.Column("last_inventory_seen_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("inventory_missing_since", sa.DateTime(timezone=True)))
        batch_op.add_column(
            sa.Column(
                "inventory_miss_count", sa.Integer(), nullable=False, server_default="0"
            )
        )
        batch_op.add_column(sa.Column("archived_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("archive_reason", sa.String(length=160)))
        batch_op.create_index(
            "ix_target_lifecycle_provider",
            ["lifecycle_status", "provider", "updated_at"],
            unique=False,
        )

    targets = sa.table(
        "targets",
        sa.column("provider", sa.String()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("last_inventory_seen_at", sa.DateTime(timezone=True)),
    )
    op.get_bind().execute(
        targets.update()
        .where(targets.c.provider != "local")
        .values(last_inventory_seen_at=targets.c.updated_at)
    )


def downgrade() -> None:
    with op.batch_alter_table("targets", schema=None) as batch_op:
        batch_op.drop_index("ix_target_lifecycle_provider")
        batch_op.drop_column("archive_reason")
        batch_op.drop_column("archived_at")
        batch_op.drop_column("inventory_miss_count")
        batch_op.drop_column("inventory_missing_since")
        batch_op.drop_column("last_inventory_seen_at")
        batch_op.drop_column("lifecycle_status")
