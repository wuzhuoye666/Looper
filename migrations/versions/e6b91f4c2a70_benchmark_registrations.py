"""add benchmark registration lifecycle

Revision ID: e6b91f4c2a70
Revises: c3f2a81d9e47
Create Date: 2026-08-22 11:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6b91f4c2a70"
down_revision: str | Sequence[str] | None = "c3f2a81d9e47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "benchmark_registrations",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("draft_json", sa.JSON(), nullable=False),
        sa.Column("constraints_json", sa.JSON(), nullable=False),
        sa.Column("manifest_digest", sa.String(length=71), nullable=True),
        sa.Column("benchmark_key", sa.String(length=180), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["benchmark_key"], ["benchmarks.key"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("benchmark_key"),
    )
    op.create_index(
        "ix_benchmark_registration_status_updated",
        "benchmark_registrations",
        ["status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_benchmark_registration_status_updated", table_name="benchmark_registrations"
    )
    op.drop_table("benchmark_registrations")
