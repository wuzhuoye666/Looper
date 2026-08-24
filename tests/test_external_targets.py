"""External target import: declaration, fail-closed validation, idempotency."""

from __future__ import annotations

import json

import pytest
from looper_api.external_targets import (
    EXTERNAL_PROVIDER,
    ConnectExternalTargetRequest,
    DiscoveredExternalTarget,
    ExternalTargetError,
    ImportExternalTargetRequest,
    connect_existing_target,
    connect_external_target,
    external_targets,
    import_external_target,
    validate_endpoint,
)
from looper_api.models import Base, TargetRecord
from looper_api.serialization import target_view
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def external_db_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
        session.rollback()
    Base.metadata.drop_all(engine)
    engine.dispose()


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


def test_import_creates_external_target(external_db_session) -> None:
    record = import_external_target(external_db_session, _request(runnable=True))
    external_db_session.flush()
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
    assert view["hardware"] == "EPYC 7B13 · 16 vCPU · 64 GiB"


def test_target_view_uses_nested_cpu_model_when_processor_is_architecture(
    external_db_session,
) -> None:
    record = import_external_target(
        external_db_session,
        _request(
            hardware={
                "processor": "x86_64",
                "logical_cpu_count": 8,
                "memory_gib": 14.72,
            }
        ),
    )
    record.fingerprint_json = {
        **record.fingerprint_json,
        "architecture": "x86_64",
        "cpu": {"model_name": "Intel(R) Xeon(R) Platinum"},
    }

    assert target_view(record)["hardware"] == (
        "Intel(R) Xeon(R) Platinum · x86_64 · 8 vCPU · 14.72 GiB"
    )


def test_import_without_worker_claims_stays_inventory_only(external_db_session) -> None:
    record = import_external_target(external_db_session, _request(runnable=False))
    external_db_session.flush()
    assert record.status == "inventory-only"
    assert record.runnable is False
    assert target_view(record)["status"] == "unknown"


def test_import_is_idempotent_by_endpoint(external_db_session) -> None:
    first = import_external_target(external_db_session, _request(name="old-name", runnable=True))
    external_db_session.flush()
    second = import_external_target(
        external_db_session,
        _request(name="new-name", runnable=False, hardware={"logical_cpu_count": 32}),
    )
    external_db_session.flush()
    assert first.id == second.id
    assert len(external_targets(external_db_session)) == 1
    refreshed = external_db_session.get(TargetRecord, first.id)
    assert refreshed.name == "new-name"
    assert refreshed.runnable is False
    assert refreshed.fingerprint_json["logical_cpu_count"] == 32


def test_import_fails_closed_on_bad_endpoint(external_db_session) -> None:
    with pytest.raises(ExternalTargetError, match="endpoint must be a valid"):
        import_external_target(external_db_session, _request(endpoint="user@10.0.0.7"))
    external_db_session.flush()
    assert external_targets(external_db_session) == []


def test_import_fails_closed_without_hardware_evidence(external_db_session) -> None:
    with pytest.raises(ExternalTargetError, match="hardware"):
        import_external_target(external_db_session, _request(hardware={}))
    external_db_session.flush()
    assert external_targets(external_db_session) == []


# --- SSH discovery -----------------------------------------------------------------


def _connection(**overrides) -> ConnectExternalTargetRequest:
    payload = {
        "endpoint": "10.0.0.8",
        "port": 22,
        "username": "ubuntu",
        "auth_method": "password",
        "password": "one-time-secret",
    }
    payload.update(overrides)
    return ConnectExternalTargetRequest.model_validate(payload)


def _discovered() -> DiscoveredExternalTarget:
    return DiscoveredExternalTarget(
        hostname="compute-01",
        operating_system="Ubuntu 24.04.2 LTS",
        kernel="Linux 6.8.0-60-generic",
        architecture="x86_64",
        processor="AMD EPYC 7B13",
        logical_cpu_count=16,
        memory_gib=62.78,
        host_key_sha256="SHA256:" + "A" * 43,
        host_key_type="ssh-ed25519",
    )


def test_connection_request_requires_credential_for_selected_method() -> None:
    with pytest.raises(ValidationError, match="password is required"):
        _connection(password=None)
    with pytest.raises(ValidationError, match="private_key is required"):
        _connection(auth_method="private-key", password=None)


def test_connection_request_normalizes_host_key_without_prefix() -> None:
    request = _connection(expected_host_key_sha256="A" * 43)
    assert request.expected_host_key_sha256 == "SHA256:" + "A" * 43


def test_connect_discovers_inventory_without_persisting_credentials(
    external_db_session,
) -> None:
    observed: list[ConnectExternalTargetRequest] = []

    def fake_probe(request: ConnectExternalTargetRequest) -> DiscoveredExternalTarget:
        observed.append(request)
        return _discovered()

    record = connect_external_target(external_db_session, _connection(), probe=fake_probe)
    external_db_session.flush()

    assert observed and observed[0].password.get_secret_value() == "one-time-secret"
    assert record.name == "compute-01"
    assert record.status == "inventory-only"
    assert record.runnable is False
    assert record.inventory_json["source"] == "ssh-discovery"
    assert record.inventory_json["auth_method"] == "password"
    assert record.inventory_json["host_key_sha256"].startswith("SHA256:")
    assert record.fingerprint_json["logical_cpu_count"] == 16
    assert record.fingerprint_json["memory_gib"] == 62.78
    assert record.fingerprint_json["architecture"] == "x86_64"
    persisted = json.dumps(
        {
            "inventory": record.inventory_json,
            "fingerprint": record.fingerprint_json,
            "capabilities": record.capabilities_json,
        }
    )
    assert "one-time-secret" not in persisted
    assert "password" in persisted  # The non-secret auth method is retained.

    view = target_view(record)
    assert view["status"] == "inventory"
    assert view["framework"] == "Ubuntu 24.04.2 LTS"
    assert view["hardware"] == "AMD EPYC 7B13 · x86_64 · 16 vCPU · 62.78 GiB"


def test_connect_does_not_persist_failed_probe(external_db_session) -> None:
    def failed_probe(_request: ConnectExternalTargetRequest) -> DiscoveredExternalTarget:
        raise ExternalTargetError("SSH authentication failed")

    with pytest.raises(ExternalTargetError, match="authentication"):
        connect_external_target(external_db_session, _connection(), probe=failed_probe)
    assert external_targets(external_db_session) == []


# --- Linux discovery command robustness --------------------------------------------


def test_discovery_command_has_cpuinfo_fallbacks() -> None:
    from looper_api.external_targets import _LINUX_DISCOVERY_COMMAND

    # aarch64 /proc/cpuinfo has no "model name"; the probe must fall back to
    # uname -p/uname -m instead of emitting an empty processor value.
    assert "model name|Hardware|Processor" in _LINUX_DISCOVERY_COMMAND
    assert "''|unknown|aarch64" in _LINUX_DISCOVERY_COMMAND
    # The operating_system probe must not rely on awk's exit status (which is
    # still 0 when no PRETTY_NAME line matches).
    assert "if [ -n \"$value\" ]" in _LINUX_DISCOVERY_COMMAND
    assert "else uname -s" in _LINUX_DISCOVERY_COMMAND

def test_parse_linux_inventory_accepts_arm64_output() -> None:
    from looper_api.external_targets import _parse_linux_inventory

    output = chr(10).join([
        "hostname=iZ7xv7pbi8h3rgoed1ume3Z",
        "operating_system=Ubuntu 26.04 LTS",
        "kernel=Linux 7.0.0-28-generic",
        "architecture=aarch64",
        "processor=aarch64",
        "logical_cpu_count=1",
        "memory_kib=1665396",
    ])

    class FakeHostKey:
        def asbytes(self) -> bytes:
            return b"k" * 32

        def get_name(self) -> str:
            return "ssh-ed25519"

    parsed = _parse_linux_inventory(output, FakeHostKey())
    assert parsed.architecture == "aarch64"
    assert parsed.processor == "aarch64"
    assert parsed.logical_cpu_count == 1
    assert parsed.operating_system == "Ubuntu 26.04 LTS"


def test_discovery_command_reports_host_capability_facts() -> None:
    from looper_api.external_targets import _LINUX_DISCOVERY_COMMAND

    for marker in (
        "os_id=",
        "os_version_id=",
        "cap_uid=",
        "cap_sudo=",
        "cap_systemd=",
        "cap_perf=",
        "cap_perl=",
        "cap_python=",
    ):
        assert marker in _LINUX_DISCOVERY_COMMAND


def test_parse_linux_inventory_captures_host_capabilities() -> None:
    from looper_api.external_targets import _parse_linux_inventory

    output = chr(10).join([
        "hostname=compute-01",
        "operating_system=Ubuntu 22.04.3 LTS",
        "kernel=Linux 5.15.0-91-generic",
        "architecture=x86_64",
        "processor=AMD EPYC 7B13",
        "logical_cpu_count=8",
        "memory_kib=15728640",
        "os_id=ubuntu",
        "os_version_id=22.04",
        "cap_uid=1000",
        "cap_sudo=1",
        "cap_systemd=1",
        "cap_perf=1",
        "cap_perl=0",
        "cap_python=1",
    ])

    class FakeHostKey:
        def asbytes(self) -> bytes:
            return b"k" * 32

        def get_name(self) -> str:
            return "ssh-ed25519"

    parsed = _parse_linux_inventory(output, FakeHostKey())
    assert {
        "linux",
        "ubuntu",
        "ubuntu-22.04",
        "local-process",
        "systemd",
        "sudo",
        "root",
        "perf",
        "python",
    } <= set(parsed.capabilities)
    assert "perl" not in parsed.capabilities


def test_connect_stores_probed_capabilities(external_db_session) -> None:
    discovered = DiscoveredExternalTarget(
        hostname="compute-01",
        operating_system="Ubuntu 22.04.3 LTS",
        kernel="Linux 5.15.0-91-generic",
        architecture="x86_64",
        processor="AMD EPYC 7B13",
        logical_cpu_count=8,
        memory_gib=15.0,
        host_key_sha256="SHA256:" + "A" * 43,
        host_key_type="ssh-ed25519",
        capabilities=["linux", "ubuntu-22.04", "systemd", "root", "local-process"],
    )

    record = connect_external_target(
        external_db_session, _connection(), probe=lambda _request: discovered
    )
    external_db_session.flush()
    assert {
        "external",
        "ssh",
        "x86_64",
        "linux",
        "ubuntu-22.04",
        "systemd",
        "root",
        "local-process",
    } <= set(record.capabilities_json)


def test_connect_existing_target_merges_probed_capabilities(external_db_session) -> None:
    record = import_external_target(
        external_db_session,
        _request(runnable=True, capabilities=["custom-role"]),
    )
    external_db_session.flush()
    discovered = DiscoveredExternalTarget(
        hostname="compute-01",
        operating_system="Ubuntu 22.04.3 LTS",
        kernel="Linux 5.15.0-91-generic",
        architecture="x86_64",
        processor="AMD EPYC 7B13",
        logical_cpu_count=8,
        memory_gib=15.0,
        host_key_sha256="SHA256:" + "A" * 43,
        host_key_type="ssh-ed25519",
        capabilities=["linux", "ubuntu-22.04", "systemd"],
    )

    refreshed = connect_existing_target(
        external_db_session,
        record,
        _connection(endpoint="10.0.0.9"),
        probe=lambda _request: discovered,
    )
    external_db_session.flush()
    merged = set(refreshed.capabilities_json)
    assert {"custom-role", "linux", "ubuntu-22.04", "systemd", "ssh", "x86_64"} <= merged
