from __future__ import annotations

from typing import Any

from looper_core.canonical import canonical_digest, utc_now
from sqlalchemy.orm import Session

from looper_api.cloud_contracts import ProviderId
from looper_api.models import TargetRecord
from looper_api.providers.utils import cloud_target_id, legacy_cloud_target_ids

_PROVIDER_CAPABILITIES = {
    ProviderId.TENCENT: "tencent-cvm",
    ProviderId.ALIBABA: "alibaba-ecs",
    ProviderId.VOLCENGINE: "volcengine-ecs",
    ProviderId.BAIDU: "baidu-bcc",
}


def adopt_cloud_target(
    session: Session,
    *,
    provider: ProviderId | str,
    region: str,
    zone: str,
    instance_id: str,
    name: str,
    instance_type: str,
    image_id: str,
    state: str,
    cpu: int | None = None,
    memory_gib: float | None = None,
    private_ip: str | None = None,
    public_ip_present: bool = False,
    vpc_id: str | None = None,
    subnet_id: str | None = None,
    source: str = "external-adoption",
) -> TargetRecord:
    normalized_provider = ProviderId(provider)
    required = {
        "region": region,
        "zone": zone,
        "instance_id": instance_id,
        "name": name,
        "instance_type": instance_type,
        "image_id": image_id,
        "state": state,
        "source": source,
    }
    for field, value in required.items():
        if not value.strip():
            raise ValueError(f"{field} is required")
    if len(source) > 80 or any(character in source for character in ("\r", "\n", "\x00")):
        raise ValueError("source is invalid")

    target_id = cloud_target_id(normalized_provider.value, region, instance_id)
    record = session.get(TargetRecord, target_id)
    if record is None:
        for legacy_id in legacy_cloud_target_ids(normalized_provider.value, region, instance_id):
            record = session.get(TargetRecord, legacy_id)
            if record is not None:
                break

    inventory: dict[str, Any] = {
        "source": source,
        "region": region,
        "zone": zone,
        "instance_id": instance_id,
        "instance_name": name,
        "instance_state": state,
        "image_id": image_id,
        "vpc_id": vpc_id,
        "subnet_id": subnet_id,
        "private_ip": private_ip,
        "public_ip_present": public_ip_present,
    }
    fingerprint: dict[str, Any] = {
        "provider": normalized_provider.value,
        "region": region,
        "zone": zone,
        "instance_id": instance_id,
        "instance_type": instance_type,
        "cpu": cpu,
        "memory_gib": memory_gib,
        "image_id": image_id,
    }
    capabilities = [_PROVIDER_CAPABILITIES[normalized_provider], "cloud-instance", "inventory"]
    now = utc_now()
    values = {
        "name": name,
        "provider": normalized_provider.value,
        "status": "inventory-only",
        "capabilities_json": capabilities,
        "inventory_json": inventory,
        "fingerprint_json": fingerprint,
        "snapshot_digest": canonical_digest(
            {
                "provider": normalized_provider.value,
                "fingerprint": fingerprint,
                "inventory": inventory,
            }
        ),
        "runnable": False,
        "lifecycle_status": "active",
        "last_inventory_seen_at": now,
        "inventory_missing_since": None,
        "inventory_miss_count": 0,
        "archived_at": None,
        "archive_reason": None,
        "updated_at": now,
    }
    if record is None:
        record = TargetRecord(id=target_id, created_at=now, **values)
        session.add(record)
    else:
        record.id = target_id
        for field, value in values.items():
            setattr(record, field, value)
    session.flush()
    return record
