from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from looper_api.config import get_settings
from sqlalchemy import create_engine, inspect


def _config(repository: Path, database: Path) -> Config:
    config = Config(str(repository / "alembic.ini"))
    config.set_main_option("script_location", str(repository / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    return config


def test_system_optimization_migration_round_trip(
    tmp_path: Path, monkeypatch
) -> None:
    repository = Path(__file__).resolve().parents[1]
    database = tmp_path / "migration.db"
    monkeypatch.setenv("LOOPER_DATABASE_URL", f"sqlite:///{database.as_posix()}")
    get_settings.cache_clear()
    config = _config(repository, database)

    command.upgrade(config, "b4c7d9e2f1a6")
    inspector = inspect(create_engine(f"sqlite:///{database.as_posix()}"))
    assert "system_optimization_studies" in inspector.get_table_names()
    assert "system_optimization_artifact_links" in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns("system_optimization_studies")} >= {
        "baseline_capacity_study_id",
        "candidate_capacity_study_id",
        "hypothesis_digest",
        "decision_digest",
        "snapshot_digest",
        "rollback_verified",
        "fencing_token",
    }

    command.downgrade(config, "e7f8a9b0c1d2")
    inspector = inspect(create_engine(f"sqlite:///{database.as_posix()}"))
    assert "system_optimization_studies" not in inspector.get_table_names()
    assert "system_optimization_artifact_links" not in inspector.get_table_names()
    assert "capacity_studies" in inspector.get_table_names()

    command.upgrade(config, "head")
    inspector = inspect(create_engine(f"sqlite:///{database.as_posix()}"))
    assert "system_optimization_studies" in inspector.get_table_names()
    get_settings.cache_clear()
