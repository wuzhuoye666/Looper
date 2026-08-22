"""persist selection frontier load points

Revision ID: c3f2a81d9e47
Revises: b7e91d42c5fa
Create Date: 2026-08-21 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f2a81d9e47"
down_revision: str | Sequence[str] | None = "b7e91d42c5fa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "selection_load_points",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("experiment_id", sa.String(length=80), nullable=False),
        sa.Column("workload_id", sa.String(length=120), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("offered_load", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("offered_load_key", sa.String(length=40), nullable=False),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("required_repeats", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("analysis_json", sa.JSON(), nullable=False),
        sa.Column("analysis_input_digest", sa.String(length=71), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "experiment_id", "workload_id", "offered_load_key", name="uq_selection_load_point"
        ),
        sa.UniqueConstraint(
            "experiment_id", "sequence", name="uq_selection_load_point_sequence"
        ),
    )
    op.create_index(
        "ix_selection_load_point_experiment_id",
        "selection_load_points",
        ["experiment_id"],
        unique=False,
    )
    op.create_index(
        "ix_selection_load_point_status",
        "selection_load_points",
        ["experiment_id", "status", "sequence"],
        unique=False,
    )

    connection = op.get_bind()
    attempts = sa.table(
        "attempts",
        sa.column("id", sa.String()),
        sa.column("experiment_id", sa.String()),
        sa.column("queue_sequence", sa.Integer()),
        sa.column("created_at", sa.DateTime()),
    )
    rows = connection.execute(
        sa.select(attempts.c.id, attempts.c.experiment_id).order_by(
            attempts.c.experiment_id,
            attempts.c.queue_sequence,
            attempts.c.created_at,
            attempts.c.id,
        )
    ).all()
    next_sequence: dict[str, int] = {}
    for attempt_id, experiment_id in rows:
        sequence = next_sequence.get(experiment_id, 0)
        connection.execute(
            attempts.update().where(attempts.c.id == attempt_id).values(queue_sequence=sequence)
        )
        next_sequence[experiment_id] = sequence + 1

    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.drop_constraint("uq_attempt_repeat_retry", type_="unique")
        batch_op.add_column(sa.Column("selection_load_point_id", sa.String(length=80)))
        batch_op.create_foreign_key(
            "fk_attempt_selection_load_point",
            "selection_load_points",
            ["selection_load_point_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_unique_constraint(
            "uq_attempt_queue_sequence", ["experiment_id", "queue_sequence"]
        )
        batch_op.create_index(
            "ix_attempt_selection_load_point",
            ["selection_load_point_id", "status", "repeat_index", "retry_index"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.drop_index("ix_attempt_selection_load_point")
        batch_op.drop_constraint("uq_attempt_queue_sequence", type_="unique")
        batch_op.drop_constraint("fk_attempt_selection_load_point", type_="foreignkey")
        batch_op.drop_column("selection_load_point_id")
        batch_op.create_unique_constraint(
            "uq_attempt_repeat_retry", ["evaluation_id", "repeat_index", "retry_index"]
        )
    op.drop_index("ix_selection_load_point_status", table_name="selection_load_points")
    op.drop_index("ix_selection_load_point_experiment_id", table_name="selection_load_points")
    op.drop_table("selection_load_points")
