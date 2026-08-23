"""add dynamic source discoveries

Revision ID: a9d7e5c3b1f0
Revises: f4a7c9d12e63
Create Date: 2026-08-23 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9d7e5c3b1f0"
down_revision: str | Sequence[str] | None = "f4a7c9d12e63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_discoveries",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column("archive_name", sa.String(length=255), nullable=False),
        sa.Column("source_digest", sa.String(length=71), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("file_manifest_json", sa.JSON(), nullable=False),
        sa.Column("excluded_files_json", sa.JSON(), nullable=False),
        sa.Column("contract_json", sa.JSON()),
        sa.Column("trace_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=80)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_source_discovery_created", "source_discoveries", ["created_at"])
    op.create_index("ix_source_discoveries_source_digest", "source_discoveries", ["source_digest"])


def downgrade() -> None:
    op.drop_index("ix_source_discoveries_source_digest", table_name="source_discoveries")
    op.drop_index("ix_source_discovery_created", table_name="source_discoveries")
    op.drop_table("source_discoveries")
