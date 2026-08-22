"""Import externally owned machines as Looper targets.

Machines purchased off-platform (on-premise servers, other clouds, bare metal)
have no provider API to sync from. This module lets an operator declare such a
machine manually so it can be used exactly like a cloud-synced target: it flows
into experiment target selection and, when a worker claims the
``target.<target_id>`` capability, becomes runnable.

Everything here is a *declaration* -- no credentials are stored, nothing is
probed. Reachability and readiness are reported by worker heartbeats, not by
this import.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from looper_core.canonical import canonical_digest, utc_now
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from looper_api.models import TargetRecord

EXTERNAL_PROVIDER = "external"
_EXTERNAL_ID_PREFIX = "external:"
_SLUG_RE = re.compile(r"[^a-z0-9.-]+")
_HOSTNAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,253}[a-z0-9])?")


class ExternalTargetError(ValueError):
    """A rejected external target import; fail closed, never fabricate."""


class ExternalHardwareSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processor: str | None = Field(default=None, max_length=160)
    logical_cpu_count: int | None = Field(default=None, ge=1, le=100000)
    memory_gib: float | None = Field(default=None, gt=0, le=100000)
    instance_type: str | None = Field(default=None, max_length=120)


class ExternalLocationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    region: str | None = Field(default=None, max_length=64)
    zone: str | None = Field(default=None, max_length=64)


class ImportExternalTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    endpoint: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    framework: str | None = Field(default=None, max_length=80)
    version: str | None = Field(default=None, max_length=80)
    hardware: ExternalHardwareSpec = Field(default_factory=ExternalHardwareSpec)
    location: ExternalLocationSpec = Field(default_factory=ExternalLocationSpec)
    capabilities: list[str] = Field(default_factory=list, max_length=50)
    runnable: bool = False


def validate_endpoint(value: str) -> str | None:
    """Return a normalized endpoint or None when it is not a valid address.

    Accepts IPv4, IPv6 or a plain hostname. Rejects paths, whitespace and
    anything that embeds a connection string or credentials.
    """

    normalized = value.strip().casefold()
    if not normalized or len(normalized) > 255:
        return None
    if any(character.isspace() for character in normalized) or "/" in normalized:
        return None
    if "@" in normalized or ":" in normalized.replace("]", ""):
        # IPv6 addresses contain colons; a user:port or user@host form does not.
        try:
            ipaddress.IPv6Address(normalized)
            return normalized
        except ValueError:
            return None
    try:
        ipaddress.ip_address(normalized)
        return normalized
    except ValueError:
        pass
    if _HOSTNAME_RE.fullmatch(normalized):
        return normalized
    return None


def _target_id(endpoint: str) -> str:
    slug = _SLUG_RE.sub("-", endpoint).strip("-")
    if slug and len(slug) <= 86:
        return f"{_EXTERNAL_ID_PREFIX}{slug}"
    digest = canonical_digest({"provider": EXTERNAL_PROVIDER, "endpoint": endpoint})
    return f"{_EXTERNAL_ID_PREFIX}{digest}"


def _validate_payload(request: ImportExternalTargetRequest) -> None:
    if not request.name.strip():
        raise ExternalTargetError("name is required")
    if request.framework and not request.framework.strip():
        raise ExternalTargetError("framework must not be blank")
    if any(not str(capability).strip() for capability in request.capabilities):
        raise ExternalTargetError("capabilities must not contain blank entries")
    if len(request.capabilities) != len({str(item) for item in request.capabilities}):
        raise ExternalTargetError("capabilities must be unique")


def import_external_target(
    session: Session, request: ImportExternalTargetRequest
) -> TargetRecord:
    """Create or update an externally owned target (idempotent by endpoint).

    The target id is derived from the normalized endpoint, so re-importing the
    same machine refreshes its declaration instead of creating a duplicate.
    """

    _validate_payload(request)
    endpoint = validate_endpoint(request.endpoint)
    if endpoint is None:
        raise ExternalTargetError(
            "endpoint must be a valid IPv4, IPv6 address or hostname"
        )
    if request.hardware.logical_cpu_count is None and request.hardware.memory_gib is None:
        raise ExternalTargetError(
            "at least one of hardware.logical_cpu_count or hardware.memory_gib is required"
        )

    target_id = _target_id(endpoint)
    now = utc_now()
    inventory: dict[str, Any] = {
        "source": "manual",
        "endpoint": endpoint,
        "description": request.description,
        "framework": request.framework,
        "version": request.version,
        "region": request.location.region,
        "zone": request.location.zone,
    }
    fingerprint = {
        "provider": EXTERNAL_PROVIDER,
        "processor": request.hardware.processor,
        "logical_cpu_count": request.hardware.logical_cpu_count,
        "memory_gib": request.hardware.memory_gib,
        "instance_type": request.hardware.instance_type,
    }
    capabilities = [EXTERNAL_PROVIDER] + sorted(
        {str(item).strip() for item in request.capabilities}
    )
    snapshot = {
        "provider": EXTERNAL_PROVIDER,
        "capabilities": capabilities,
        "fingerprint": fingerprint,
    }
    status = "available" if request.runnable else "inventory-only"
    record = session.get(TargetRecord, target_id)
    if record is None:
        record = TargetRecord(
            id=target_id,
            name=request.name.strip(),
            provider=EXTERNAL_PROVIDER,
            status=status,
            capabilities_json=capabilities,
            inventory_json=inventory,
            fingerprint_json=fingerprint,
            snapshot_digest=canonical_digest(snapshot),
            runnable=request.runnable,
            lifecycle_status="active",
            last_inventory_seen_at=now,
            inventory_missing_since=None,
            inventory_miss_count=0,
            archived_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(record)
    else:
        record.name = request.name.strip()
        record.provider = EXTERNAL_PROVIDER
        record.status = status
        record.capabilities_json = capabilities
        record.inventory_json = inventory
        record.fingerprint_json = fingerprint
        record.snapshot_digest = canonical_digest(snapshot)
        record.runnable = request.runnable
        record.lifecycle_status = "active"
        record.last_inventory_seen_at = now
        record.inventory_missing_since = None
        record.inventory_miss_count = 0
        record.updated_at = now
    return record


def external_targets(session: Session) -> list[TargetRecord]:
    return list(
        session.scalars(
            select(TargetRecord).where(TargetRecord.provider == EXTERNAL_PROVIDER)
        )
    )
