"""enforce one cloud order per quote

Revision ID: a4c8e2f91b73
Revises: d8f2c1b7a4e6
Create Date: 2026-08-20 22:20:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a4c8e2f91b73"
down_revision: str | Sequence[str] | None = "d8f2c1b7a4e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cloud_orders", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_cloud_order_quote", ["quote_id"])


def downgrade() -> None:
    with op.batch_alter_table("cloud_orders", schema=None) as batch_op:
        batch_op.drop_constraint("uq_cloud_order_quote", type_="unique")
