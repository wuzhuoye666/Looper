from __future__ import annotations

from collections.abc import Generator

import pytest
from looper_api.models import Base, TargetRecord
from looper_api.seed import seed_system
from looper_core.canonical import canonical_digest, utc_now
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture(autouse=True)
def isolate_explicit_cloud_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LOOPER_ENV_FILE", str(tmp_path / "isolated-cloud.env"))
    monkeypatch.setenv("LOOPER_LIVE_PURCHASE_ENABLED", "false")
    monkeypatch.setenv("LOOPER_LIVE_PURCHASE_PROVIDERS", "")
    monkeypatch.setenv("LOOPER_OPERATOR_TOKEN", "")
    monkeypatch.setenv(
        "LOOPER_PURCHASE_CONFIRMATION_SECRET", "change-me-before-enabling-live-purchase"
    )
    monkeypatch.setenv("LOOPER_MAX_LIVE_HOURLY_AMOUNT", "10")


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def configure(connection: object, _record: object) -> None:
        cursor = connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        seed_system(session)
        # Most scheduler unit tests exercise a synthetic local execution target.
        # Production seeding intentionally no longer creates this record.
        fingerprint = {
            "system": "Linux",
            "architecture": "x86_64",
            "logical_cpu_count": 16,
            "memory_gib": 64,
        }
        capabilities = ["python", "local-process", "linux", "x86_64"]
        session.add(TargetRecord(
            id="local",
            name="Test local target",
            provider="fixture",
            status="available",
            capabilities_json=capabilities,
            inventory_json={"source": "test"},
            fingerprint_json=fingerprint,
            snapshot_digest=canonical_digest({"fingerprint": fingerprint}),
            runnable=True,
            lifecycle_status="active",
            created_at=utc_now(),
            updated_at=utc_now(),
        ))
        session.commit()
        yield session
        session.rollback()
    Base.metadata.drop_all(engine)
    engine.dispose()
