"""Remove the seeded local workstation and experiments that reference it.

Revision ID: e1a6b5c4d3f2
Revises: d2c4e6f8a1b3
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1a6b5c4d3f2"
down_revision: str | Sequence[str] | None = "d2c4e6f8a1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def remove_local_workstation(connection: sa.Connection) -> None:
    experiment_ids = {
        row[0]
        for row in connection.execute(
            sa.text("SELECT DISTINCT experiment_id FROM evaluations WHERE target_id = :target_id"),
            {"target_id": "local"},
        )
    }
    experiments = sa.table(
        "experiments",
        sa.column("id", sa.String),
        sa.column("spec_json", sa.JSON),
    )
    for experiment_id, spec in connection.execute(
        sa.select(experiments.c.id, experiments.c.spec_json)
    ):
        if isinstance(spec, dict) and "local" in (spec.get("target_ids") or []):
            experiment_ids.add(experiment_id)
    if experiment_ids:
        ordered_ids = sorted(experiment_ids)
        parameters = {f"experiment_{index}": value for index, value in enumerate(ordered_ids)}
        placeholders = ", ".join(f":experiment_{index}" for index in range(len(experiment_ids)))
        attempt_ids = (
            f"SELECT id FROM attempts WHERE experiment_id IN ({placeholders})"
        )
        for table in ("artifact_links", "checks", "observations"):
            connection.execute(
                sa.text(f"DELETE FROM {table} WHERE attempt_id IN ({attempt_ids})"),
                parameters,
            )
        for table in (
            "attempts",
            "evaluations",
            "selection_load_points",
            "candidates",
            "analysis_snapshots",
            "events",
        ):
            connection.execute(
                sa.text(f"DELETE FROM {table} WHERE experiment_id IN ({placeholders})"),
                parameters,
            )
        connection.execute(
            sa.text(f"DELETE FROM experiments WHERE id IN ({placeholders})"),
            parameters,
        )
    connection.execute(sa.text("DELETE FROM targets WHERE id = :target_id"), {"target_id": "local"})


def upgrade() -> None:
    remove_local_workstation(op.get_bind())


def downgrade() -> None:
    # Historical experiments and evidence cannot be reconstructed safely.
    pass
