"""add system optimization study orchestration

Revision ID: b4c7d9e2f1a6
Revises: e7f8a9b0c1d2
Create Date: 2026-08-24 17:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4c7d9e2f1a6"
down_revision: str | Sequence[str] | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "system_optimization_studies",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column(
            "baseline_capacity_study_id",
            sa.String(length=80),
            sa.ForeignKey("capacity_studies.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "candidate_capacity_study_id",
            sa.String(length=80),
            sa.ForeignKey("capacity_studies.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "target_id",
            sa.String(length=100),
            sa.ForeignKey("targets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("network", sa.String(length=16), nullable=False),
        sa.Column("minimum_effect", sa.Float(), nullable=False),
        sa.Column("authorization_profile_digest", sa.String(length=71), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("fencing_token", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("hypothesis_digest", sa.String(length=71)),
        sa.Column("decision_digest", sa.String(length=71)),
        sa.Column("snapshot_digest", sa.String(length=71)),
        sa.Column(
            "rollback_verified", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("orchestration_json", sa.JSON(), nullable=False),
        sa.Column("activation_json", sa.JSON(), nullable=False),
        sa.Column("problem_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_system_optimization_status_updated",
        "system_optimization_studies",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_system_optimization_baseline_created",
        "system_optimization_studies",
        ["baseline_capacity_study_id", "created_at"],
    )
    op.create_table(
        "system_optimization_artifact_links",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column(
            "study_id",
            sa.String(length=80),
            sa.ForeignKey("system_optimization_studies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "digest",
            sa.String(length=71),
            sa.ForeignKey("artifacts.digest", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=160), nullable=False),
        sa.Column("producer", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "study_id",
            "digest",
            "role",
            "name",
            name="uq_system_optimization_artifact_link",
        ),
    )
    op.create_index(
        "ix_system_optimization_artifact_study",
        "system_optimization_artifact_links",
        ["study_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_system_optimization_artifact_study",
        table_name="system_optimization_artifact_links",
    )
    op.drop_table("system_optimization_artifact_links")
    op.drop_index(
        "ix_system_optimization_baseline_created",
        table_name="system_optimization_studies",
    )
    op.drop_index(
        "ix_system_optimization_status_updated",
        table_name="system_optimization_studies",
    )
    op.drop_table("system_optimization_studies")
