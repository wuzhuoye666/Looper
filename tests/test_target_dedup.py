"""Deduplication of external vs cloud-inventory targets for the same machine.

One physical machine can be discovered twice: over SSH as an external target
(usually through its public address) and through provider inventory (usually
through its private address). The cloud record must win, inherit the verified
SSH facts, and the external record must be archived so the candidate resource
page lists each machine exactly once. Endpoints must prefer the public IP so
the platform can actually reach the machine.
"""

from __future__ import annotations

import pytest
from looper_api.external_targets import (
    ConnectExternalTargetRequest,
    DiscoveredExternalTarget,
    connect_external_target,
    reconcile_external_duplicate,
)
from looper_api.models import Base, TargetRecord
from looper_api.serialization import target_view
from looper_core.canonical import canonical_digest, utc_now
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

_SSH_HOST_KEY = "SHA256:" + "A" * 43


@pytest.fixture
def dedup_session() -> Session:
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


def _cloud_record(
    session: Session,
    *,
    instance_id: str = "i-7xv1fc2pmggfdftlu37j",
    private_ip: str | None = "172.28.106.37",
    public_ip: str | None = "8.148.238.132",
    provider: str = "alibaba",
) -> TargetRecord:
    identifier = f"cloud:{provider}:cn-guangzhou:{instance_id}"
    inventory = {
        "source": "cloud-inventory",
        "region": "cn-guangzhou",
        "zone": "cn-guangzhou-b",
        "instance_id": instance_id,
        "instance_name": "发布页",
        "instance_state": "Running",
        "image_id": "ubuntu_24_04_x64_20G_alibase_20240608.vhd",
        "vpc_id": "vpc-1",
        "subnet_id": "vsw-1",
        "private_ip": private_ip,
        "public_ip": public_ip,
        "endpoint": public_ip or private_ip,
        "public_ip_present": bool(public_ip),
    }
    fingerprint = {
        "provider": provider,
        "region": "cn-guangzhou",
        "zone": "cn-guangzhou-b",
        "instance_type": "ecs.e-c1m1.large",
        "cpu": 2,
        "memory_gib": 2.0,
        "image_id": "ubuntu_24_04_x64_20G_alibase_20240608.vhd",
        "os_name": "Ubuntu 24.04",
    }
    record = TargetRecord(
        id=identifier,
        name="发布页",
        provider=provider,
        status="inventory-only",
        capabilities_json=["alibaba-ecs", "inventory"],
        inventory_json=inventory,
        fingerprint_json=fingerprint,
        snapshot_digest=canonical_digest(
            {
                "provider": provider,
                "capabilities": ["alibaba-ecs", "inventory"],
                "fingerprint": fingerprint,
            }
        ),
        runnable=False,
        lifecycle_status="active",
        last_inventory_seen_at=utc_now(),
        inventory_missing_since=None,
        inventory_miss_count=0,
        archived_at=None,
        archive_reason=None,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(record)
    session.flush()
    return record


def _external_record(
    session: Session,
    *,
    endpoint: str = "8.148.238.132",
    verified: bool = True,
) -> TargetRecord:
    fingerprint = {
        "provider": "external",
        "processor": "Intel(R) Xeon(R) Platinum 8269CY CPU @ 2.50GHz",
        "logical_cpu_count": 2,
        "memory_gib": 2.05,
    }
    inventory: dict = {"source": "ssh-discovery" if verified else "manual", "endpoint": endpoint}
    if verified:
        fingerprint.update(
            system="Ubuntu 24.04.4 LTS",
            release="6.8.0-137-generic",
            architecture="x86_64",
            host_key_sha256=_SSH_HOST_KEY,
            host_key_type="ssh-ed25519",
        )
        inventory.update(
            architecture="x86_64",
            host_key_sha256=_SSH_HOST_KEY,
            host_key_type="ssh-ed25519",
        )
    record = TargetRecord(
        id=f"external:{endpoint}",
        name="iZ7xv1fc2pmggfdftlu37jZ",
        provider="external",
        status="inventory-only",
        capabilities_json=["external", "ssh", "x86_64"],
        inventory_json=inventory,
        fingerprint_json=fingerprint,
        snapshot_digest=canonical_digest(
            {
                "provider": "external",
                "capabilities": ["external", "ssh", "x86_64"],
                "fingerprint": fingerprint,
            }
        ),
        runnable=False,
        lifecycle_status="active",
        last_inventory_seen_at=utc_now(),
        inventory_missing_since=None,
        inventory_miss_count=0,
        archived_at=None,
        archive_reason=None,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(record)
    session.flush()
    return record


def _probe(_request: ConnectExternalTargetRequest) -> DiscoveredExternalTarget:
    return DiscoveredExternalTarget(
        hostname="iZ7xv1fc2pmggfdftlu37jZ",
        operating_system="Ubuntu 24.04.4 LTS",
        kernel="6.8.0-137-generic",
        architecture="x86_64",
        processor="Intel(R) Xeon(R) Platinum 8269CY CPU @ 2.50GHz",
        logical_cpu_count=2,
        memory_gib=2.05,
        host_key_sha256=_SSH_HOST_KEY,
        host_key_type="ssh-ed25519",
    )


def _connect_request(endpoint: str) -> ConnectExternalTargetRequest:
    return ConnectExternalTargetRequest(
        endpoint=endpoint,
        port=22,
        username="root",
        auth_method="private-key",
        private_key=SecretStr("private-key-material"),
        deploy_worker=False,
        remember_credentials=False,
    )


# --- Cloud sync reconciliation -------------------------------------------------------


def test_reconcile_absorbs_verified_external_twin(dedup_session) -> None:
    cloud = _cloud_record(dedup_session)
    twin = _external_record(dedup_session, endpoint="8.148.238.132")

    assert reconcile_external_duplicate(dedup_session, cloud) is True
    dedup_session.flush()

    reloaded = dedup_session.get(TargetRecord, cloud.id)
    archived = dedup_session.get(TargetRecord, twin.id)
    # Cloud record kept its provider identity plus the verified SSH facts.
    assert reloaded.provider == "alibaba"
    assert reloaded.lifecycle_status == "active"
    assert reloaded.fingerprint_json["system"] == "Ubuntu 24.04.4 LTS"
    assert reloaded.fingerprint_json["host_key_sha256"] == _SSH_HOST_KEY
    assert reloaded.fingerprint_json["instance_type"] == "ecs.e-c1m1.large"
    assert reloaded.inventory_json["endpoint"] == "8.148.238.132"
    # External duplicate archived so the resource page shows a single row.
    assert archived.lifecycle_status == "archived"
    assert archived.archive_reason == "superseded-by-cloud-inventory"
    assert archived.status == "offline"
    assert archived.runnable is False
    active_external = [
        record
        for record in dedup_session.scalars(
            select(TargetRecord).where(TargetRecord.provider == "external")
        )
        if record.lifecycle_status == "active"
    ]
    assert active_external == []


def test_reconcile_archives_unverified_manual_external_without_merging(dedup_session) -> None:
    cloud = _cloud_record(dedup_session)
    manual = _external_record(dedup_session, endpoint="8.148.238.132", verified=False)

    assert reconcile_external_duplicate(dedup_session, cloud) is True
    reloaded = dedup_session.get(TargetRecord, cloud.id)
    archived = dedup_session.get(TargetRecord, manual.id)
    assert reloaded.fingerprint_json.get("host_key_sha256") is None
    assert archived.lifecycle_status == "archived"


def test_reconcile_skips_when_fields_do_not_match(dedup_session) -> None:
    cloud = _cloud_record(dedup_session, public_ip=None, private_ip="172.28.106.37")
    other = _external_record(dedup_session, endpoint="8.138.5.244")

    assert reconcile_external_duplicate(dedup_session, cloud) is False
    assert cloud.lifecycle_status == "active"
    assert cloud.fingerprint_json.get("host_key_sha256") is None
    assert other.lifecycle_status == "active"


def test_merge_is_idempotent(dedup_session) -> None:
    cloud = _cloud_record(dedup_session)
    _external_record(dedup_session)
    assert reconcile_external_duplicate(dedup_session, cloud) is True
    # A second pass finds the external record archived and does nothing.
    assert reconcile_external_duplicate(dedup_session, cloud) is False


# --- Endpoint preference -------------------------------------------------------------


def test_target_view_prefers_public_ip_when_endpoint_missing(dedup_session) -> None:
    cloud = _cloud_record(dedup_session)
    cloud.inventory_json = {**cloud.inventory_json, "endpoint": None}
    view = target_view(cloud)
    assert view["endpoint"] == "8.148.238.132"


def test_target_view_falls_back_to_private_ip(dedup_session) -> None:
    cloud = _cloud_record(dedup_session, public_ip=None, private_ip="172.28.106.37")
    view = target_view(cloud)
    assert view["endpoint"] == "172.28.106.37"


# --- SSH discovery of an already-known cloud machine ---------------------------------


def test_connect_external_returns_cloud_twin_when_known(dedup_session) -> None:
    cloud = _cloud_record(dedup_session)
    external_id = "external:8.148.238.132"
    assert dedup_session.get(TargetRecord, external_id) is None

    record = connect_external_target(
        dedup_session, _connect_request("8.148.238.132"), probe=_probe
    )
    dedup_session.flush()

    assert record.id == cloud.id
    assert record.provider == "alibaba"
    assert record.fingerprint_json["host_key_sha256"] == _SSH_HOST_KEY
    archived = dedup_session.get(TargetRecord, external_id)
    assert archived is not None
    assert archived.lifecycle_status == "archived"
    assert archived.archive_reason == "superseded-by-cloud-inventory"


def test_connect_external_keeps_external_when_not_in_cloud(dedup_session) -> None:
    record = connect_external_target(
        dedup_session, _connect_request("8.138.5.244"), probe=_probe
    )
    dedup_session.flush()

    assert record.id == "external:8.138.5.244"
    assert record.provider == "external"
    assert record.lifecycle_status == "active"
    assert record.fingerprint_json["host_key_sha256"] == _SSH_HOST_KEY
