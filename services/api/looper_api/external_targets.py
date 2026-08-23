"""Discover and import externally owned machines as Looper targets.

The preferred path makes one bounded SSH connection, reads a small Linux
inventory, and persists only the discovered inventory and SSH host-key
fingerprint. Passwords, private keys and passphrases are request-scoped and are
never written to the database. Discovery proves inventory reachability, not
worker readiness; the target remains inventory-only until a worker claims it.

The older declaration contract remains available for API compatibility.
"""

from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import re
from collections.abc import Callable
from typing import Any, Literal

from looper_core.canonical import canonical_digest, utc_now
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from looper_api.events import append_event
from looper_api.models import TargetRecord

EXTERNAL_PROVIDER = "external"
_EXTERNAL_ID_PREFIX = "external:"
_SLUG_RE = re.compile(r"[^a-z0-9.-]+")
_HOSTNAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,253}[a-z0-9])?")


class ExternalTargetError(ValueError):
    """A rejected external target import; fail closed, never fabricate."""

    status_code = 422
    code = "external_target_error"


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


class ConnectExternalTargetRequest(BaseModel):
    """Ephemeral SSH credentials plus the address to discover."""

    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)
    auth_method: Literal["password", "private-key", "ssh-agent"] = "password"
    password: SecretStr | None = None
    private_key: SecretStr | None = None
    passphrase: SecretStr | None = None
    expected_host_key_sha256: str | None = None
    timeout_seconds: int = Field(default=10, ge=3, le=30)
    deploy_worker: bool = True
    remember_credentials: bool = True

    @field_validator("expected_host_key_sha256", mode="before")
    @classmethod
    def normalize_host_key_fingerprint(cls, value: object) -> object:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized:
            return None
        if not normalized.startswith("SHA256:"):
            normalized = f"SHA256:{normalized}"
        if re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", normalized) is None:
            raise ValueError("host key fingerprint must be SHA256 followed by 43 base64 characters")
        return normalized

    @model_validator(mode="after")
    def validate_authentication(self) -> ConnectExternalTargetRequest:
        if not self.username.strip():
            raise ValueError("username must not be blank")
        if self.auth_method == "password" and not (
            self.password and self.password.get_secret_value()
        ):
            raise ValueError("password is required for password authentication")
        if self.auth_method == "private-key" and not (
            self.private_key and self.private_key.get_secret_value()
        ):
            raise ValueError("private_key is required for private-key authentication")
        return self


class DiscoveredExternalTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str = Field(min_length=1, max_length=160)
    operating_system: str = Field(min_length=1, max_length=160)
    kernel: str = Field(min_length=1, max_length=160)
    architecture: str = Field(min_length=1, max_length=80)
    processor: str = Field(min_length=1, max_length=160)
    logical_cpu_count: int = Field(ge=1, le=100000)
    memory_gib: float = Field(gt=0, le=100000)
    host_key_sha256: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")
    host_key_type: str = Field(min_length=1, max_length=80)


_LINUX_DISCOVERY_COMMAND = "\n".join(
    (
        "LC_ALL=C",
        "printf 'hostname='; (hostname 2>/dev/null || uname -n)",
        (
            "printf 'operating_system='; (value=$(awk -F= "
            "'$1==\"PRETTY_NAME\" {value=substr($0,index($0,\"=\")+1); "
            "gsub(/^\"|\"$/, \"\", value); print value; exit}' "
            "/etc/os-release 2>/dev/null); "
            "if [ -n \"$value\" ]; then printf '%s\\n' \"$value\"; else uname -s; fi)"
        ),
        "printf 'kernel='; uname -sr",
        "printf 'architecture='; uname -m",
        (
            "printf 'processor='; (value=$(awk -F: '/^(model name|Hardware|Processor)/ "
            "{value=$2; sub(/^[[:space:]]+/, \"\", value); "
            "if (length(value)>0) {print value; exit}}' /proc/cpuinfo 2>/dev/null); "
            "if [ -n \"$value\" ]; then printf '%s\\n' \"$value\"; "
            "else p=$(uname -p 2>/dev/null); "
            "case \"$p\" in ''|unknown|aarch64) printf '%s\\n' \"$(uname -m)\" ;; "
            "*) printf '%s\\n' \"$p\" ;; esac; fi)"
        ),
        (
            "printf 'logical_cpu_count='; "
            "(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null)"
        ),
        "printf 'memory_kib='; awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo",
    )
)


def _clean_probe_value(value: str, maximum: int) -> str:
    normalized = " ".join(value.replace("\x00", "").split())
    return normalized[:maximum]


def _parse_linux_inventory(output: str, host_key: Any) -> DiscoveredExternalTarget:
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {
            "hostname",
            "operating_system",
            "kernel",
            "architecture",
            "processor",
            "logical_cpu_count",
            "memory_kib",
        }:
            values[key] = _clean_probe_value(value, 200)
    required = {
        "hostname",
        "operating_system",
        "kernel",
        "architecture",
        "processor",
        "logical_cpu_count",
        "memory_kib",
    }
    if required - values.keys() or any(not values[item] for item in required):
        raise ExternalTargetError(
            "connected, but the machine did not return complete Linux inventory"
        )
    try:
        logical_cpu_count = int(values["logical_cpu_count"])
        memory_gib = round(int(values["memory_kib"]) / 1024 / 1024, 2)
    except (TypeError, ValueError) as error:
        raise ExternalTargetError("connected, but CPU or memory inventory was invalid") from error
    digest = base64.b64encode(hashlib.sha256(host_key.asbytes()).digest()).decode().rstrip("=")
    return DiscoveredExternalTarget(
        hostname=values["hostname"],
        operating_system=values["operating_system"],
        kernel=values["kernel"],
        architecture=values["architecture"],
        processor=values["processor"],
        logical_cpu_count=logical_cpu_count,
        memory_gib=memory_gib,
        host_key_sha256=f"SHA256:{digest}",
        host_key_type=str(host_key.get_name()),
    )


def _private_key(value: str, passphrase: str | None) -> Any:
    import paramiko

    errors: list[Exception] = []
    key_types = [paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey]
    for key_type in key_types:
        try:
            return key_type.from_private_key(io.StringIO(value), password=passphrase)
        except (paramiko.SSHException, ValueError) as error:
            errors.append(error)
    raise ExternalTargetError("private key format or passphrase is invalid") from errors[-1]


def open_ssh_client(request: ConnectExternalTargetRequest) -> Any:
    """Open a pinned, request-scoped SSH connection without persisting credentials."""
    import paramiko

    endpoint = validate_endpoint(request.endpoint)
    if endpoint is None:
        raise ExternalTargetError("endpoint must be a valid IPv4, IPv6 address or hostname")

    class EphemeralHostKeyPolicy(paramiko.MissingHostKeyPolicy):
        """Accept an unknown key only for this request, optionally pinning it."""

        def missing_host_key(self, _client: Any, _hostname: str, key: Any) -> None:
            digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode()
            observed = f"SHA256:{digest.rstrip('=')}"
            if (
                request.expected_host_key_sha256
                and request.expected_host_key_sha256 != observed
            ):
                raise ExternalTargetError(
                    "SSH host key fingerprint does not match the expected value"
                )

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(EphemeralHostKeyPolicy())
    connect_options: dict[str, Any] = {
        "hostname": endpoint,
        "port": request.port,
        "username": request.username.strip(),
        "timeout": request.timeout_seconds,
        "banner_timeout": request.timeout_seconds,
        "auth_timeout": request.timeout_seconds,
    }
    if request.auth_method == "password":
        connect_options.update(
            password=request.password.get_secret_value() if request.password else None,
            allow_agent=False,
            look_for_keys=False,
        )
    elif request.auth_method == "private-key":
        passphrase = request.passphrase.get_secret_value() if request.passphrase else None
        connect_options.update(
            pkey=_private_key(request.private_key.get_secret_value(), passphrase),
            allow_agent=False,
            look_for_keys=False,
        )
    else:
        connect_options.update(allow_agent=True, look_for_keys=True)
    try:
        client.connect(**connect_options)
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise ExternalTargetError("SSH connection did not become active")
        host_key = transport.get_remote_server_key()
        digest = base64.b64encode(hashlib.sha256(host_key.asbytes()).digest()).decode().rstrip("=")
        observed = f"SHA256:{digest}"
        if request.expected_host_key_sha256 and request.expected_host_key_sha256 != observed:
            raise ExternalTargetError("SSH host key fingerprint does not match the expected value")
        return client
    except ExternalTargetError:
        client.close()
        raise
    except paramiko.AuthenticationException as error:
        client.close()
        raise ExternalTargetError(
            "SSH authentication failed; check the username and credential"
        ) from error
    except (TimeoutError, paramiko.SSHException, OSError) as error:
        client.close()
        raise ExternalTargetError(
            "SSH connection failed; check the address, port and SSH service"
        ) from error


def probe_ssh_target(request: ConnectExternalTargetRequest) -> DiscoveredExternalTarget:
    """Connect once, run a fixed read-only inventory command, then disconnect."""

    import paramiko

    client = open_ssh_client(request)
    try:
        transport = client.get_transport()
        if transport is None:
            raise ExternalTargetError("SSH connection did not become active")
        host_key = transport.get_remote_server_key()
        _stdin, stdout, stderr = client.exec_command(
            _LINUX_DISCOVERY_COMMAND, timeout=request.timeout_seconds
        )
        output = stdout.read(65537)
        error_output = stderr.read(4097)
        if len(output) > 65536 or len(error_output) > 4096:
            raise ExternalTargetError("machine inventory response exceeded the safety limit")
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            raise ExternalTargetError("connected, but the Linux inventory command failed")
        return _parse_linux_inventory(output.decode("utf-8", errors="replace"), host_key)
    except ExternalTargetError:
        raise
    except (TimeoutError, paramiko.SSHException, OSError) as error:
        raise ExternalTargetError(
            "SSH connection failed; check the address, port and SSH service"
        ) from error
    finally:
        client.close()


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


def connect_external_target(
    session: Session,
    request: ConnectExternalTargetRequest,
    probe: Callable[[ConnectExternalTargetRequest], DiscoveredExternalTarget] = probe_ssh_target,
) -> TargetRecord:
    """Discover a target over SSH and persist only verified, non-secret facts."""

    endpoint = validate_endpoint(request.endpoint)
    if endpoint is None:
        raise ExternalTargetError("endpoint must be a valid IPv4, IPv6 address or hostname")
    discovered = probe(request)
    record = import_external_target(
        session,
        ImportExternalTargetRequest(
            name=discovered.hostname,
            endpoint=endpoint,
            framework=discovered.operating_system,
            version=discovered.kernel,
            hardware=ExternalHardwareSpec(
                processor=discovered.processor,
                logical_cpu_count=discovered.logical_cpu_count,
                memory_gib=discovered.memory_gib,
            ),
            capabilities=["ssh", discovered.architecture.casefold()],
            runnable=False,
        ),
    )
    record.inventory_json = {
        **record.inventory_json,
        "source": "ssh-discovery",
        "endpoint": endpoint,
        "port": request.port,
        "username": request.username.strip(),
        "auth_method": request.auth_method,
        "architecture": discovered.architecture,
        "host_key_sha256": discovered.host_key_sha256,
        "host_key_type": discovered.host_key_type,
    }
    record.fingerprint_json = {
        **record.fingerprint_json,
        "system": discovered.operating_system,
        "release": discovered.kernel,
        "architecture": discovered.architecture,
        "host_key_sha256": discovered.host_key_sha256,
        "host_key_type": discovered.host_key_type,
    }
    record.snapshot_digest = canonical_digest(
        {
            "provider": EXTERNAL_PROVIDER,
            "capabilities": record.capabilities_json,
            "fingerprint": record.fingerprint_json,
        }
    )
    # When the probed machine is already known from the provider inventory, keep
    # the cloud record (richer identity) and archive the external duplicate.
    cloud_twin = _cloud_twin_for_endpoint(session, endpoint)
    if cloud_twin is not None:
        merge_external_duplicate(session, cloud_twin, record)
        return cloud_twin
    return record


def connect_existing_target(
    session: Session,
    target: TargetRecord,
    request: ConnectExternalTargetRequest,
    probe: Callable[[ConnectExternalTargetRequest], DiscoveredExternalTarget] = probe_ssh_target,
) -> TargetRecord:
    """Probe SSH and merge verified machine facts into an existing target."""

    endpoint = validate_endpoint(request.endpoint)
    if endpoint is None:
        raise ExternalTargetError("endpoint must be a valid IPv4, IPv6 address or hostname")
    if target.lifecycle_status != "active":
        raise ExternalTargetError("target is not active")

    discovered = probe(request)
    now = utc_now()
    target.inventory_json = {
        **target.inventory_json,
        "source": "ssh-discovery",
        "endpoint": endpoint,
        "port": request.port,
        "username": request.username.strip(),
        "auth_method": request.auth_method,
        "architecture": discovered.architecture,
        "host_key_sha256": discovered.host_key_sha256,
        "host_key_type": discovered.host_key_type,
        "instance_state": "RUNNING",
    }
    target.fingerprint_json = {
        **target.fingerprint_json,
        "system": discovered.operating_system,
        "release": discovered.kernel,
        "processor": discovered.processor,
        "logical_cpu_count": discovered.logical_cpu_count,
        "memory_gib": discovered.memory_gib,
        "architecture": discovered.architecture,
        "host_key_sha256": discovered.host_key_sha256,
        "host_key_type": discovered.host_key_type,
    }
    target.snapshot_digest = canonical_digest(
        {
            "provider": target.provider,
            "capabilities": target.capabilities_json,
            "fingerprint": target.fingerprint_json,
            "inventory": target.inventory_json,
        }
    )
    target.status = "inventory-only"
    target.runnable = False
    target.last_inventory_seen_at = now
    target.inventory_missing_since = None
    target.inventory_miss_count = 0
    target.updated_at = now
    return target


def external_targets(session: Session) -> list[TargetRecord]:
    return list(
        session.scalars(
            select(TargetRecord).where(TargetRecord.provider == EXTERNAL_PROVIDER)
        )
    )


# Cloud inventory providers whose targets can also be probed externally.
_CLOUD_PROVIDERS = {"alibaba", "tencent", "baidu", "volcengine"}
_DEDUP_ARCHIVE_REASON = "superseded-by-cloud-inventory"

# Fingerprint facts produced by a verified SSH probe that are safe to inherit
# from the external record into its cloud twin when merging duplicates.
# Provider identity (instance_type, cpu, region, zone, image_id) always stays
# authoritative from the cloud inventory.
_MERGED_FINGERPRINT_KEYS = (
    "system",
    "release",
    "processor",
    "logical_cpu_count",
    "memory_gib",
    "architecture",
    "host_key_sha256",
    "host_key_type",
)


def _external_verified(record: TargetRecord) -> bool:
    return (
        (record.inventory_json or {}).get("source") == "ssh-discovery"
        or bool((record.fingerprint_json or {}).get("host_key_sha256"))
    )


def merge_external_duplicate(
    session: Session,
    cloud_record: TargetRecord,
    external_record: TargetRecord,
    *,
    credential_store: Any | None = None,
) -> bool:
    """Fold a duplicate external target into its cloud inventory twin.

    One physical machine can be discovered twice: once over SSH as an external
    target (usually through its public address) and once through the provider
    inventory (usually through its private address). After the merge the cloud
    record keeps the provider identity and inherits the verified SSH facts,
    while the external record is archived so the candidate resource page lists
    each machine exactly once.

    Only verified SSH discoveries are inherited; manually declared external
    records are archived without touching cloud facts. When a credential store
    is provided, remembered credentials move from the external id to the cloud
    id so automated SSH tests and worker recovery keep working.
    """
    if (
        cloud_record is None
        or external_record is None
        or cloud_record.id == external_record.id
        or cloud_record.provider not in _CLOUD_PROVIDERS
        or external_record.lifecycle_status != "active"
    ):
        return False
    now = utc_now()
    verified = _external_verified(external_record)
    if verified:
        external_fingerprint = external_record.fingerprint_json or {}
        fingerprint = cloud_record.fingerprint_json or {}
        changed = False
        for key in _MERGED_FINGERPRINT_KEYS:
            value = external_fingerprint.get(key)
            if value not in (None, ""):
                fingerprint[key] = value
                changed = True
        if changed:
            cloud_record.fingerprint_json = fingerprint
            cloud_record.snapshot_digest = canonical_digest(
                {
                    "provider": cloud_record.provider,
                    "capabilities": cloud_record.capabilities_json,
                    "fingerprint": fingerprint,
                    "inventory": cloud_record.inventory_json,
                }
            )
            cloud_record.updated_at = now
        if credential_store is not None:
            host_key = str(fingerprint.get("host_key_sha256") or "")
            if host_key.startswith("SHA256:"):
                try:
                    request = credential_store.load(external_record.id)
                    credential_store.save(cloud_record.id, request, host_key)
                except Exception:
                    # Credential migration is best-effort; the operator can
                    # re-enter the key once through the SSH dialog.
                    pass
    external_record.lifecycle_status = "archived"
    external_record.status = "offline"
    external_record.runnable = False
    external_record.inventory_missing_since = None
    external_record.archived_at = external_record.archived_at or now
    external_record.archive_reason = _DEDUP_ARCHIVE_REASON
    external_record.updated_at = now
    append_event(
        session,
        experiment_id=None,
        event_type="target.external_duplicate_merged",
        entity_type="target",
        entity_id=cloud_record.id,
        idempotency_key=(
            f"target-external-duplicate-merged:{cloud_record.id}:{external_record.id}"
        ),
        payload={
            "cloudTargetId": cloud_record.id,
            "externalTargetId": external_record.id,
            "verified": verified,
        },
    )
    return True


def external_twin_for_cloud(
    session: Session, cloud_record: TargetRecord
) -> TargetRecord | None:
    """Return the single active external target describing the same machine."""
    inventory = cloud_record.inventory_json or {}
    endpoints = {inventory.get("public_ip"), inventory.get("private_ip")} - {None}
    if not endpoints:
        return None
    matches = [
        record
        for record in external_targets(session)
        if record.lifecycle_status == "active"
        and (record.inventory_json or {}).get("endpoint") in endpoints
    ]
    return matches[0] if len(matches) == 1 else None


def reconcile_external_duplicate(
    session: Session,
    cloud_record: TargetRecord,
    *,
    credential_store: Any | None = None,
) -> bool:
    """Archive a duplicate external twin of a cloud inventory record, if any."""
    twin = external_twin_for_cloud(session, cloud_record)
    if twin is None:
        return False
    return merge_external_duplicate(
        session, cloud_record, twin, credential_store=credential_store
    )


def _cloud_twin_for_endpoint(session: Session, endpoint: str) -> TargetRecord | None:
    matches: list[TargetRecord] = []
    for record in session.scalars(
        select(TargetRecord).where(TargetRecord.provider.in_(sorted(_CLOUD_PROVIDERS)))
    ):
        if record.lifecycle_status != "active":
            continue
        inventory = record.inventory_json or {}
        if inventory.get("public_ip") == endpoint or inventory.get("private_ip") == endpoint:
            matches.append(record)
    return matches[0] if len(matches) == 1 else None
