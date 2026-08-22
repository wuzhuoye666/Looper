"""cloud catalog, quote, and order workflow

Revision ID: d8f2c1b7a4e6
Revises: 9c42392dedd5
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8f2c1b7a4e6"
down_revision: str | Sequence[str] | None = "9c42392dedd5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cloud_catalog_cache",
        sa.Column("key", sa.String(length=71), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("region", sa.String(length=64), nullable=True),
        sa.Column("zone", sa.String(length=64), nullable=True),
        sa.Column("query_json", sa.JSON(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )
    with op.batch_alter_table("cloud_catalog_cache") as batch:
        batch.create_index(
            "ix_cloud_catalog_provider_kind",
            ["provider", "resource_type", "expires_at"],
            unique=False,
        )
        batch.create_index("ix_cloud_catalog_cache_provider", ["provider"], unique=False)

    op.create_table(
        "cloud_quotes",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("spec_json", sa.JSON(), nullable=False),
        sa.Column("spec_digest", sa.String(length=71), nullable=False),
        sa.Column("provider_quote_id", sa.String(length=180), nullable=True),
        sa.Column("hourly_amount", sa.Numeric(20, 8), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("estimated", sa.Boolean(), nullable=False),
        sa.Column("quote_digest", sa.String(length=71), nullable=False),
        sa.Column("provider_details_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("quote_digest"),
    )
    with op.batch_alter_table("cloud_quotes") as batch:
        batch.create_index("ix_cloud_quotes_provider", ["provider"], unique=False)
        batch.create_index(
            "ix_cloud_quote_provider_created", ["provider", "created_at"], unique=False
        )

    op.create_table(
        "cloud_orders",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("quote_id", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("client_token", sa.String(length=64), nullable=False),
        sa.Column("spec_json", sa.JSON(), nullable=False),
        sa.Column("spec_digest", sa.String(length=71), nullable=False),
        sa.Column("quote_digest", sa.String(length=71), nullable=False),
        sa.Column("hourly_amount", sa.Numeric(20, 8), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("confirmation_phrase_hash", sa.String(length=64), nullable=False),
        sa.Column("confirmation_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_order_id", sa.String(length=180), nullable=True),
        sa.Column("provider_instance_ids_json", sa.JSON(), nullable=False),
        sa.Column("provider_response_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["quote_id"], ["cloud_quotes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("client_token"),
    )
    with op.batch_alter_table("cloud_orders") as batch:
        batch.create_index("ix_cloud_orders_quote_id", ["quote_id"], unique=False)
        batch.create_index("ix_cloud_orders_provider", ["provider"], unique=False)
        batch.create_index(
            "ix_cloud_order_status_created", ["status", "created_at"], unique=False
        )


def downgrade() -> None:
    op.drop_table("cloud_orders")
    op.drop_table("cloud_quotes")
    op.drop_table("cloud_catalog_cache")
