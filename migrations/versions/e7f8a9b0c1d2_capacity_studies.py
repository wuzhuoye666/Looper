"""add retained source archives and capacity studies

Revision ID: e7f8a9b0c1d2
Revises: e1a6b5c4d3f2
Create Date: 2026-08-24 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7f8a9b0c1d2"
down_revision: str | Sequence[str] | None = "e1a6b5c4d3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "source_discoveries", sa.Column("archive_retained_until", sa.DateTime(timezone=True))
    )
    op.add_column(
        "source_discoveries", sa.Column("archive_deleted_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "source_discoveries", sa.Column("archive_delete_reason", sa.String(length=80))
    )
    op.create_table(
        "capacity_studies",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column(
            "discovery_id",
            sa.String(length=80),
            sa.ForeignKey("source_discoveries.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("current_step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("draft_json", sa.JSON(), nullable=False),
        sa.Column("preflight_json", sa.JSON(), nullable=False),
        sa.Column("execution_json", sa.JSON(), nullable=False),
        sa.Column("report_json", sa.JSON()),
        sa.Column("error_code", sa.String(length=80)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_capacity_study_status_updated", "capacity_studies", ["status", "updated_at"]
    )
    op.create_index(
        "ix_capacity_study_discovery", "capacity_studies", ["discovery_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_capacity_study_discovery", table_name="capacity_studies")
    op.drop_index("ix_capacity_study_status_updated", table_name="capacity_studies")
    op.drop_table("capacity_studies")
    op.drop_column("source_discoveries", "archive_delete_reason")
    op.drop_column("source_discoveries", "archive_deleted_at")
    op.drop_column("source_discoveries", "archive_retained_until")
