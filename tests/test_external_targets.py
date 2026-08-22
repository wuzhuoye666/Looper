"""External target import: declaration, fail-closed validation, idempotency."""

from __future__ import annotations

import pytest
from looper_api.external_targets import (
    EXTERNAL_PROVIDER,
    ExternalTargetError,
    ImportExternalTargetRequest,
    external_targets,
    import_external_target,
    validate_endpoint,
)
from looper_api.models import TargetRecord
from looper_api.serialization import target_view
from pydantic import ValidationError


def _request(**overrides) -> ImportExternalTargetRequest:
    payload: dict = {
        "name": "onprem-db-01",
        "endpoint": "10.0.0.7",
        "description": "自有机房数据库节点",
        "hardware": {"logical_cpu_count": 16, "memory_gib": 64, "processor": "EPYC 7B13"},
        "location": {"region": "onprem-bj"},
    }
    payload.update(overrides)
    return ImportExternalTargetRequest.model_validate(payload)


# --- Endpoint validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("10.0.0.7", "10.0.0.7"),
        ("2001:db8::1", "2001:db8::1"),
        ("db-01.internal", "db-01.internal"),
        ("", None),
        ("10.0.0.7/24", None),
        ("user@10.0.0.7", None),
        ("host with space", None),
        ("10.0.0.7:22", None),
    ],
)
def test_validate_endpoint_accepts_only_plain_addresses(value, expected) -> None:
    assert validate_endpoint(value) == expected


# --- Contract validation ------------------------------------------------------------


def test_request_rejects_blank_name() -> None:
    with pytest.raises(ValidationError):
        _request(name="")


def test_request_rejects_invalid_hardware() -> None:
    with pytest.raises(ValidationError):
        _request(hardware={"logical_cpu_count": 0})
    with pytest.raises(ValidationError):
        _request(hardware={"memory_gib": -1})


def test_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _request(secret="do-not-store")


# --- Import behaviour ---------------------------------------------------------------


def test_import_creates_external_target(db_session) -> None:
    record = import_external_target(db_session, _request(runnable=True))
    db_session.flush()
    assert isinstance(record, TargetRecord)
    assert record.provider == EXTERNAL_PROVIDER
    assert record.id.startswith("external:")
    assert record.status == "available"
    assert record.runnable is True
    assert record.lifecycle_status == "active"
    assert record.inventory_json["source"] == "manual"
    assert record.inventory_json["endpoint"] == "10.0.0.7"
    assert record.fingerprint_json["logical_cpu_count"] == 16
    assert "external" in record.capabilities_json

    view = target_view(record)
    assert view["endpoint"] == "10.0.0.7"
    assert view["status"] == "online"
    assert view["runnable"] is True
    assert view["hardware"] == "EPYC 7B13 · 16 vCPU"


def test_import_without_worker_claims_stays_inventory_only(db_session) -> None:
    record = import_external_target(db_session, _request(runnable=False))
    db_session.flush()
    assert record.status == "inventory-only"
    assert record.runnable is False
    assert target_view(record)["status"] == "unknown"


def test_import_is_idempotent_by_endpoint(db_session) -> None:
    first = import_external_target(db_session, _request(name="old-name", runnable=True))
    db_session.flush()
    second = import_external_target(
        db_session,
        _request(name="new-name", runnable=False, hardware={"logical_cpu_count": 32}),
    )
    db_session.flush()
    assert first.id == second.id
    assert len(external_targets(db_session)) == 1
    refreshed = db_session.get(TargetRecord, first.id)
    assert refreshed.name == "new-name"
    assert refreshed.runnable is False
    assert refreshed.fingerprint_json["logical_cpu_count"] == 32


def test_import_fails_closed_on_bad_endpoint(db_session) -> None:
    with pytest.raises(ExternalTargetError, match="endpoint must be a valid"):
        import_external_target(db_session, _request(endpoint="user@10.0.0.7"))
    db_session.flush()
    assert external_targets(db_session) == []


def test_import_fails_closed_without_hardware_evidence(db_session) -> None:
    with pytest.raises(ExternalTargetError, match="hardware"):
        import_external_target(db_session, _request(hardware={}))
    db_session.flush()
    assert external_targets(db_session) == []
