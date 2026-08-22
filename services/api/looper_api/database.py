from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from looper_api.config import get_settings
from looper_api.models import Base

settings = get_settings()
logger = logging.getLogger(__name__)

_LEGACY_REVISION = "9c42392dedd5"
_CLOUD_SCHEMA_REVISION = "d8f2c1b7a4e6"
_CLOUD_TABLES = {"cloud_catalog_cache", "cloud_quotes", "cloud_orders"}
_PRE_REGISTRATION_REVISION = "c3f2a81d9e47"
_REGISTRATION_TABLES = {"benchmark_registrations"}

if settings.database_uri.startswith("sqlite:///"):
    database_path = Path(settings.database_uri.removeprefix("sqlite:///"))
    database_path.parent.mkdir(parents=True, exist_ok=True)

engine: Engine = create_engine(
    settings.database_uri,
    connect_args={"check_same_thread": False, "timeout": 15}
    if settings.database_uri.startswith("sqlite")
    else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


if settings.database_uri.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()


def _migration_config() -> Config:
    repository = Path(__file__).resolve().parents[3]
    config = Config(str(repository / "alembic.ini"))
    config.set_main_option("script_location", str(repository / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_uri.replace("%", "%%"))
    return config


def _adopt_unversioned_schema(config: Config) -> None:
    with engine.connect() as connection:
        inspector = inspect(connection)
        all_tables = set(inspector.get_table_names())
        application_tables = all_tables - {"alembic_version"}
        has_revision = "alembic_version" in all_tables and bool(
            connection.scalar(text("SELECT COUNT(*) FROM alembic_version"))
        )
        if has_revision or not application_tables:
            return

        current_tables = set(Base.metadata.tables)
        legacy_tables = current_tables - _CLOUD_TABLES
        pre_registration_tables = current_tables - _REGISTRATION_TABLES
        if application_tables == legacy_tables - _REGISTRATION_TABLES:
            target = _LEGACY_REVISION
        elif application_tables == pre_registration_tables:
            target = _PRE_REGISTRATION_REVISION
        elif application_tables == current_tables:
            quote_constraints = inspector.get_unique_constraints("cloud_orders")
            has_unique_quote = any(
                set(constraint.get("column_names") or []) == {"quote_id"}
                for constraint in quote_constraints
            )
            target = "head" if has_unique_quote else _CLOUD_SCHEMA_REVISION
        else:
            missing = sorted(current_tables - application_tables)
            extra = sorted(application_tables - current_tables)
            raise RuntimeError(
                "database has an unmanaged or partial schema; "
                f"missing tables={missing}, unexpected tables={extra}"
            )

        for table_name in application_tables:
            actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
            expected_columns = set(Base.metadata.tables[table_name].columns.keys())
            if actual_columns != expected_columns:
                raise RuntimeError(
                    f"database table {table_name!r} does not match the managed schema; "
                    f"missing columns={sorted(expected_columns - actual_columns)}, "
                    f"unexpected columns={sorted(actual_columns - expected_columns)}"
                )

    logger.warning("Adopting verified pre-Alembic database schema at revision %s", target)
    command.stamp(config, target)


def init_database() -> None:
    config = _migration_config()
    _adopt_unversioned_schema(config)
    command.upgrade(config, "head")


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
