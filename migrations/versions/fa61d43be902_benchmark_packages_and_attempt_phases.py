"""add managed benchmark packages and attempt phases

Revision ID: fa61d43be902
Revises: f4a7c9d12e63
Create Date: 2026-08-23 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fa61d43be902"
down_revision: str | Sequence[str] | None = "f4a7c9d12e63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("benchmarks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("package_digest", sa.String(length=71)))
    with op.batch_alter_table("benchmark_registrations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("package_digest", sa.String(length=71)))
        batch_op.add_column(sa.Column("package_path", sa.Text()))
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("phase", sa.String(length=40)))
        batch_op.add_column(sa.Column("phase_detail", sa.String(length=500)))


def downgrade() -> None:
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.drop_column("phase_detail")
        batch_op.drop_column("phase")
    with op.batch_alter_table("benchmark_registrations", schema=None) as batch_op:
        batch_op.drop_column("package_path")
        batch_op.drop_column("package_digest")
    with op.batch_alter_table("benchmarks", schema=None) as batch_op:
        batch_op.drop_column("package_digest")
