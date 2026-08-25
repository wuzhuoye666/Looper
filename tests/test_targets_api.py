from __future__ import annotations

from looper_api.app import list_targets
from looper_api.config import Settings
from looper_api.models import TargetRecord
from looper_api.seed import seed_system


def test_system_seed_does_not_recreate_local_target(db_session, tmp_path) -> None:
    local = db_session.get(TargetRecord, "local")
    db_session.delete(local)
    db_session.flush()
    seed_system(db_session)
    db_session.flush()

    result = list_targets(
        db_session,
        Settings(data_dir=tmp_path / "looper-data"),
        include_inactive=False,
    )

    assert result["total"] == 0
    assert db_session.get(TargetRecord, "local") is None


def test_target_view_exposes_registered_private_and_public_ips(db_session) -> None:
    from looper_api.serialization import target_view
    from looper_core.canonical import utc_now

    record = TargetRecord(
        id="cloud:test",
        name="PHP",
        provider="alibaba",
        status="available",
        capabilities_json=[],
        inventory_json={
            "private_ip": "10.0.0.72",
            "public_ip": "39.108.178.0",
            "endpoint": "39.108.178.0",
        },
        fingerprint_json={},
        snapshot_digest="sha256:test",
        runnable=True,
        lifecycle_status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db_session.add(record)
    db_session.flush()

    view = target_view(record)
    assert view["privateIp"] == "10.0.0.72"
    assert view["publicIp"] == "39.108.178.0"
    assert view["endpoint"] == "39.108.178.0"
