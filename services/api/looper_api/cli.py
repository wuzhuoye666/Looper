from __future__ import annotations

import json
import os
import platform
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import typer
from looper_core.action_loop import VerificationPolicy
from looper_core.adapters import load_and_apply_adapter
from looper_core.canonical import canonical_digest
from looper_core.manifest import load_and_validate_manifest
from looper_core.system_opt.config_manifest import (
    ConfigItem,
    ConfigManifest,
    parse_config_manifest_yaml,
)
from looper_core.system_opt.demo import run_full_demo
from looper_core.system_opt.domain import (
    AuthorizedDomain,
    DomainEvidence,
    resolve_domain,
)
from looper_core.system_opt.executor import ConfigSnapshot
from looper_core.system_opt.executor.local_linux import LocalLinuxBackend
from looper_core.system_opt.executor.runner import SubprocessCommandRunner
from looper_core.system_opt.executor.simulated import SimulatedBackend
from looper_core.system_opt.inventory import (
    LinuxDiscoveryPolicy,
    LinuxRawCollector,
    LocalToolInventoryCollector,
    ManifestInventoryCollector,
    capture_environment_fingerprint,
    parse_tool_requirements_yaml,
)
from looper_core.system_opt.lease import (
    FileTargetGuard,
    ReconciliationOutcome,
    TargetReconciliation,
    TargetRecoveryEvidence,
)
from looper_core.system_opt.measurement import (
    CommandMeasurementAdapter,
    MeasurementCommandSpec,
)
from looper_core.system_opt.policy import (
    OptimizationMode,
    parse_optimization_policy_yaml,
)
from looper_core.system_opt.safety import SafetyController, SafetyPolicy, SafetyState
from looper_core.system_opt.scoring import MeasurementBatch
from looper_core.system_opt.state_evidence import (
    OWNERSHIP_DECLARATION_SCHEMA,
    ConfigurationStateEvidence,
    LinuxExactAssignmentCollector,
    OwnershipDeclaration,
)
from looper_core.system_opt.tuning import SystemOptimizationEngine
from pydantic import TypeAdapter
from rich.console import Console
from sqlalchemy import select

from looper_api.cloud_adoption import adopt_cloud_target
from looper_api.cloud_setup import configure_cloud_purchase, credential_fields
from looper_api.database import init_database, session_scope
from looper_api.evidence import verify_evidence_bundle
from looper_api.models import ExperimentRecord
from looper_api.scheduler import create_demo_request, create_experiment, start_experiment
from looper_api.seed import seed_system
from looper_api.source_manager import (
    SourcePolicyError,
    fetch_source,
    load_source_lock,
    resolve_source,
)
from looper_api.verified_demo import run_verified_compression_loop

app = typer.Typer(help="Looper control-plane utilities")
benchmark_app = typer.Typer(help="Benchmark contract tools")
adapter_app = typer.Typer(help="Benchmark adapter tools")
evidence_app = typer.Typer(help="Evidence bundle tools")
source_app = typer.Typer(help="Third-party source governance")
demo_app = typer.Typer(help="Local demo experiment")
cloud_app = typer.Typer(help="Multi-cloud runtime configuration")
system_opt_app = typer.Typer(help="Linux System Optimizer closed-loop tools")
app.add_typer(benchmark_app, name="benchmark")
app.add_typer(adapter_app, name="adapter")
app.add_typer(evidence_app, name="evidence")
app.add_typer(source_app, name="source")
app.add_typer(demo_app, name="demo")
app.add_typer(cloud_app, name="cloud")
app.add_typer(system_opt_app, name="system-opt")
console = Console()
error_console = Console(stderr=True)


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=False)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _load_state_evidence(path: Path) -> ConfigurationStateEvidence:
    return ConfigurationStateEvidence.model_validate(_read_json(path))


def _load_reconciliation(path: Path | None) -> TargetReconciliation | None:
    if path is None:
        return None
    return TargetReconciliation.model_validate(_read_json(path))


def _current_environment_digest() -> str:
    fingerprint = capture_environment_fingerprint()
    return canonical_digest(fingerprint.model_dump(mode="json"))


def _manifest_items_for_snapshot(
    manifest: ConfigManifest, snapshot: ConfigSnapshot
) -> list[ConfigItem]:
    by_id = {item.id: item for item in manifest.items}
    unknown = sorted(set(snapshot.entries) - set(by_id))
    if unknown:
        raise typer.BadParameter(f"snapshot references unknown manifest items: {unknown}")
    if not snapshot.entries:
        raise typer.BadParameter("snapshot must contain at least one manifest item")
    return [by_id[item_id] for item_id in sorted(snapshot.entries)]


def _require_linux_confirmation(enable_real: bool, confirmation: str) -> None:
    if platform.system().lower() != "linux":
        raise typer.BadParameter("real System Optimizer writes require Linux")
    if not enable_real or confirmation != "I_UNDERSTAND_LINUX_CONFIG_WRITES":
        raise typer.BadParameter(
            "real writes require --enable-real and the exact confirmation token"
        )


def _local_backend(
    manifest: ConfigManifest,
    *,
    target_id: str,
    allowed_executables: list[str],
    writable_roots: list[Path],
) -> LocalLinuxBackend:
    if platform.system().lower() != "linux":
        raise typer.BadParameter("local-linux backend requires Linux")
    if not allowed_executables:
        raise typer.BadParameter("at least one --allow-executable is required")
    declared = {
        command.argv[0]
        for item in manifest.items
        for command in (item.read.command, item.apply)
        if command is not None
    }
    missing = sorted(declared - set(allowed_executables))
    if missing:
        raise typer.BadParameter(f"manifest executables are not allowlisted: {missing}")
    runner = SubprocessCommandRunner(
        allowed_executables=set(allowed_executables),
        writable_file_roots=writable_roots,
    )
    privileged = hasattr(os, "geteuid") and os.geteuid() == 0
    return LocalLinuxBackend(
        target_id=target_id,
        enabled=True,
        runner=runner,
        system_name="linux",
        privileged=privileged,
    )


@system_opt_app.command("validate")
def validate_system_optimizer_contracts(
    manifest_path: Path = typer.Option(..., "--manifest", exists=True, dir_okay=False),
    policy_path: Path = typer.Option(..., "--policy", exists=True, dir_okay=False),
) -> None:
    manifest = parse_config_manifest_yaml(manifest_path.read_text(encoding="utf-8"))
    policy = parse_optimization_policy_yaml(policy_path.read_text(encoding="utf-8"))
    console.print_json(
        json.dumps(
            {
                "valid": True,
                "manifest_id": manifest.id,
                "manifest_digest": manifest.digest,
                "policy_id": policy.id,
                "policy_digest": canonical_digest(policy.model_dump(mode="json")),
                "target_os": "linux",
            }
        )
    )


@system_opt_app.command("demo")
def run_system_optimizer_demo(
    output: Path = typer.Option(..., "--output", dir_okay=False),
) -> None:
    result = run_full_demo()
    _write_json(output, result)
    console.print_json(
        json.dumps(
            {
                "evidence_kind": result.evidence_kind,
                "warning": result.warning,
                "general_stop": result.general.stop_reason,
                "general_recommended": result.general.recommended_candidate_id,
                "workload_stop": result.workload.stop_reason,
                "workload_routed_components": result.workload.routed_components,
                "workload_recommended": result.workload.recommended_candidate_id,
                "output": str(output.resolve()),
            }
        )
    )


@system_opt_app.command("inventory")
def collect_system_inventory(
    manifest_path: Path = typer.Option(..., "--manifest", exists=True, dir_okay=False),
    backend_kind: str = typer.Option(..., "--backend"),
    target_id: str = typer.Option(..., "--target-id"),
    output: Path = typer.Option(..., "--output", dir_okay=False),
    initial_state: Path | None = typer.Option(None, "--initial-state", exists=True, dir_okay=False),
    allow_executable: list[str] | None = typer.Option(None, "--allow-executable"),
    state_evidence_path: Path | None = typer.Option(
        None, "--state-evidence", exists=True, dir_okay=False
    ),
) -> None:
    manifest = parse_config_manifest_yaml(manifest_path.read_text(encoding="utf-8"))
    if backend_kind == "simulated":
        if initial_state is None:
            raise typer.BadParameter("simulated inventory requires --initial-state")
        payload = _read_json(initial_state)
        if not isinstance(payload, dict):
            raise typer.BadParameter("initial state must be a JSON object")
        backend = SimulatedBackend(payload, target_id=target_id)
    elif backend_kind == "local-linux":
        backend = _local_backend(
            manifest,
            target_id=target_id,
            allowed_executables=allow_executable or [],
            writable_roots=[],
        )
    else:
        raise typer.BadParameter("backend must be simulated or local-linux")
    state_evidence = (
        _load_state_evidence(state_evidence_path) if state_evidence_path is not None else None
    )
    result = ManifestInventoryCollector().collect(
        manifest,
        backend,
        fencing_token=0,
        state_evidence=state_evidence,
    )
    _write_json(output, result)
    console.print_json(
        json.dumps(
            {
                "target_id": result.target_id,
                "target_os": result.target_os,
                "collector_environment": result.metadata.collector_environment.model_dump(
                    mode="json"
                ),
                "count": len(result.items),
                "counting_basis": result.counting_basis,
                "digest": result.digest,
                "output": str(output.resolve()),
            }
        )
    )


@system_opt_app.command("state-inventory")
def collect_linux_configuration_state(
    manifest_path: Path = typer.Option(..., "--manifest", exists=True, dir_okay=False),
    target_id: str = typer.Option(..., "--target-id"),
    source: list[Path] = typer.Option(..., "--source", exists=True, dir_okay=False),
    output: Path = typer.Option(..., "--output", dir_okay=False),
) -> None:
    manifest = parse_config_manifest_yaml(manifest_path.read_text(encoding="utf-8"))
    environment_digest = _current_environment_digest()
    result = LinuxExactAssignmentCollector().collect(
        manifest,
        target_id=target_id,
        environment_digest=environment_digest,
        source_paths=source,
        collected_at=datetime.now(UTC),
    )
    _write_json(output, result)
    console.print_json(
        json.dumps(
            {
                "target_id": result.target_id,
                "manifest_digest": result.manifest_digest,
                "state_evidence_digest": result.digest,
                "source_count": len(result.source_scope),
                "assignment_count": len(result.assignments),
                "record_count": len(result.records),
                "counting_basis": result.counting_basis,
                "output": str(output.resolve()),
            }
        )
    )


@system_opt_app.command("reconcile-expired-lease")
def reconcile_expired_target_lease(
    manifest_path: Path = typer.Option(..., "--manifest", exists=True, dir_okay=False),
    expected_snapshot_path: Path = typer.Option(
        ..., "--expected-snapshot", exists=True, dir_okay=False
    ),
    target_id: str = typer.Option(..., "--target-id"),
    lease_root: Path = typer.Option(..., "--lease-root", file_okay=False),
    allow_executable: list[str] = typer.Option(..., "--allow-executable"),
    output: Path = typer.Option(..., "--output", dir_okay=False),
) -> None:
    manifest = parse_config_manifest_yaml(manifest_path.read_text(encoding="utf-8"))
    expected = ConfigSnapshot.model_validate(_read_json(expected_snapshot_path))
    if expected.target_id != target_id:
        raise typer.BadParameter("expected snapshot target does not match --target-id")
    snapshot_items = _manifest_items_for_snapshot(manifest, expected)
    backend = _local_backend(
        manifest,
        target_id=target_id,
        allowed_executables=allow_executable,
        writable_roots=[],
    )
    guard = FileTargetGuard(lease_root)
    existing = guard.current_lease(target_id)
    now = datetime.now(UTC)
    if existing is None:
        raise typer.BadParameter("target has no lease to reconcile")
    if existing.expires_at > now:
        raise typer.BadParameter("target lease has not expired")
    actual = backend.snapshot(snapshot_items, fencing_token=existing.fencing_token)
    matched = actual.complete and expected.complete and actual.digest == expected.digest
    result = TargetReconciliation(
        target_id=target_id,
        previous_lease_digest=existing.digest,
        actual_snapshot=actual,
        expected_snapshot=expected,
        outcome=(
            ReconciliationOutcome.MATCHED_SNAPSHOT
            if matched
            else ReconciliationOutcome.NEEDS_ATTENTION
        ),
        reason=(
            "actual target snapshot matches the expected recovery snapshot"
            if matched
            else "actual target snapshot is incomplete or differs from expected recovery state"
        ),
        recorded_at=now,
    )
    _write_json(output, result)
    if not matched:
        guard.mark_needs_attention(
            target_id,
            reason=result.reason,
            evidence_digest=result.digest,
            now=now,
        )
    console.print_json(
        json.dumps(
            {
                "target_id": target_id,
                "outcome": result.outcome,
                "actual_snapshot_digest": result.actual_snapshot_digest,
                "expected_snapshot_digest": result.expected_snapshot_digest,
                "reconciliation_digest": result.digest,
                "output": str(output.resolve()),
            }
        )
    )
    if not matched:
        raise typer.Exit(code=2)


@system_opt_app.command("authorize-state")
def authorize_configuration_state(
    state_evidence_path: Path = typer.Option(..., "--state-evidence", exists=True, dir_okay=False),
    actor_id: str = typer.Option(..., "--actor-id"),
    declared_by: str = typer.Option(..., "--declared-by"),
    item_id: list[str] = typer.Option(..., "--item-id"),
    reason: str = typer.Option(..., "--reason"),
    output: Path = typer.Option(..., "--output", dir_okay=False),
) -> None:
    source = _load_state_evidence(state_evidence_path)
    current_environment = _current_environment_digest()
    if source.environment_digest != current_environment:
        raise typer.BadParameter(
            "state evidence environment digest does not match the current host"
        )
    declaration = OwnershipDeclaration(
        schema_version=OWNERSHIP_DECLARATION_SCHEMA,
        target_id=source.target_id,
        manifest_digest=source.manifest_digest,
        environment_digest=source.environment_digest,
        source_evidence_digest=source.digest,
        actor_id=actor_id,
        declared_by=declared_by,
        item_ids=item_id,
        reason=reason,
        declared_at=datetime.now(UTC),
    )
    result = source.apply_ownership_declaration(declaration)
    _write_json(output, result)
    console.print_json(
        json.dumps(
            {
                "target_id": result.target_id,
                "actor_id": actor_id,
                "authorized_item_ids": item_id,
                "source_evidence_digest": source.digest,
                "declaration_digest": declaration.digest,
                "state_evidence_digest": result.digest,
                "output": str(output.resolve()),
            }
        )
    )


@system_opt_app.command("recover-attention")
def recover_target_attention(
    manifest_path: Path = typer.Option(..., "--manifest", exists=True, dir_okay=False),
    approved_snapshot_path: Path = typer.Option(
        ..., "--approved-snapshot", exists=True, dir_okay=False
    ),
    target_id: str = typer.Option(..., "--target-id"),
    lease_root: Path = typer.Option(..., "--lease-root", file_okay=False),
    allow_executable: list[str] = typer.Option(..., "--allow-executable"),
    output: Path = typer.Option(..., "--output", dir_okay=False),
) -> None:
    manifest = parse_config_manifest_yaml(manifest_path.read_text(encoding="utf-8"))
    approved = ConfigSnapshot.model_validate(_read_json(approved_snapshot_path))
    if approved.target_id != target_id:
        raise typer.BadParameter("approved snapshot target does not match --target-id")
    snapshot_items = _manifest_items_for_snapshot(manifest, approved)
    backend = _local_backend(
        manifest,
        target_id=target_id,
        allowed_executables=allow_executable,
        writable_roots=[],
    )
    guard = FileTargetGuard(lease_root)
    attention = guard.current_attention(target_id)
    if attention is None:
        raise typer.BadParameter("target has no attention record")
    actual = backend.snapshot(snapshot_items, fencing_token=0)
    if not actual.complete or not approved.complete or actual.digest != approved.digest:
        raise typer.BadParameter(
            "actual target snapshot is incomplete or differs from the approved snapshot"
        )
    result = TargetRecoveryEvidence(
        target_id=target_id,
        attention_evidence_digest=attention.evidence_digest,
        actual_snapshot=actual,
        approved_snapshot=approved,
        reason="actual target snapshot matches the operator-approved recovery state",
        recorded_at=datetime.now(UTC),
    )
    _write_json(output, result)
    guard.clear_attention(target_id, recovery=result)
    console.print_json(
        json.dumps(
            {
                "target_id": target_id,
                "recovery_digest": result.digest,
                "attention_cleared": True,
                "output": str(output.resolve()),
            }
        )
    )


@system_opt_app.command("raw-inventory")
def collect_linux_raw_inventory(
    root: list[Path] = typer.Option(..., "--root", file_okay=False),
    max_files: int = typer.Option(..., "--max-files", min=1),
    max_bytes_per_file: int = typer.Option(..., "--max-bytes-per-file", min=1),
    output: Path = typer.Option(..., "--output", dir_okay=False),
) -> None:
    result = LinuxRawCollector().collect(
        LinuxDiscoveryPolicy(
            roots=root,
            max_files=max_files,
            max_bytes_per_file=max_bytes_per_file,
        )
    )
    _write_json(output, result)
    console.print_json(
        json.dumps(
            {
                "target_os": result.target_os,
                "collector_environment": result.metadata.collector_environment.model_dump(
                    mode="json"
                ),
                "enumeration_complete": result.enumeration_complete,
                "all_values_readable": result.all_values_readable,
                "complete": result.complete,
                "count": len(result.records),
                "counting_basis": result.counting_basis,
                "digest": result.digest,
                "output": str(output.resolve()),
            }
        )
    )


@system_opt_app.command("tool-inventory")
def collect_linux_tool_inventory(
    requirements_path: Path = typer.Option(..., "--requirements", exists=True, dir_okay=False),
    output: Path = typer.Option(..., "--output", dir_okay=False),
) -> None:
    requirements = parse_tool_requirements_yaml(requirements_path.read_text(encoding="utf-8"))
    result = LocalToolInventoryCollector().collect(requirements)
    _write_json(output, result)
    console.print_json(
        json.dumps(
            {
                "target_os": result.target_os,
                "collector_environment": result.metadata.collector_environment.model_dump(
                    mode="json"
                ),
                "critical_executables_resolved": (result.critical_executables_resolved),
                "verification_scope": result.verification_scope,
                "critical_missing": result.critical_missing,
                "count": len(result.items),
                "counting_basis": result.counting_basis,
                "digest": result.digest,
                "output": str(output.resolve()),
            }
        )
    )


@system_opt_app.command("manual")
def apply_manual_system_configuration(
    manifest_path: Path = typer.Option(..., "--manifest", exists=True, dir_okay=False),
    changes_path: Path = typer.Option(..., "--changes", exists=True, dir_okay=False),
    target_id: str = typer.Option(..., "--target-id"),
    owner_id: str = typer.Option(..., "--owner-id"),
    lease_root: Path = typer.Option(..., "--lease-root", file_okay=False),
    lease_ttl_seconds: float = typer.Option(..., "--lease-ttl-seconds", min=1),
    max_changes: int = typer.Option(..., "--max-changes", min=1, max=100),
    max_changes_reason: str | None = typer.Option(None, "--max-changes-reason"),
    allow_executable: list[str] = typer.Option(..., "--allow-executable"),
    writable_root: list[Path] = typer.Option(..., "--writable-root", file_okay=False),
    state_evidence_path: Path = typer.Option(..., "--state-evidence", exists=True, dir_okay=False),
    reconciliation_evidence_path: Path | None = typer.Option(
        None, "--reconciliation-evidence", exists=True, dir_okay=False
    ),
    keep: bool = typer.Option(False, "--keep"),
    authorize_keep: bool = typer.Option(False, "--authorize-keep"),
    enable_real: bool = typer.Option(False, "--enable-real"),
    confirmation: str = typer.Option("", "--confirmation"),
    output: Path = typer.Option(..., "--output", dir_okay=False),
) -> None:
    _require_linux_confirmation(enable_real, confirmation)
    manifest = parse_config_manifest_yaml(manifest_path.read_text(encoding="utf-8"))
    changes = _read_json(changes_path)
    if not isinstance(changes, dict):
        raise typer.BadParameter("changes must be a JSON object")
    backend = _local_backend(
        manifest,
        target_id=target_id,
        allowed_executables=allow_executable,
        writable_roots=writable_root,
    )
    state_evidence = _load_state_evidence(state_evidence_path)
    pinned_items, ownership_unknown_items = state_evidence.safety_constraints(
        manifest,
        target_id=target_id,
        actor_id=owner_id,
        environment_digest=_current_environment_digest(),
    )
    guard = FileTargetGuard(lease_root)
    lease = guard.acquire(
        target_id,
        owner_id,
        ttl_seconds=lease_ttl_seconds,
        now=datetime.now(UTC),
        reconciliation=_load_reconciliation(reconciliation_evidence_path),
    )
    try:
        result = SafetyController(
            SafetyPolicy(
                max_changes=max_changes,
                max_changes_reason=max_changes_reason,
                pinned_items=pinned_items,
                ownership_unknown_items=ownership_unknown_items,
                high_risk_waivers=set(),
                allow_keep=keep,
                require_privileged=True,
            )
        ).execute(
            manifest,
            changes,
            backend,
            fencing_token=lease.fencing_token,
            keep=keep,
            keep_authorized=authorize_keep,
        )
        _write_json(output, result)
        if result.state == SafetyState.NEEDS_ATTENTION:
            guard.mark_needs_attention(
                target_id,
                reason=result.reason or "manual transaction needs attention",
                evidence_digest=canonical_digest(result.model_dump(mode="json")),
                now=datetime.now(UTC),
            )
    finally:
        guard.release(lease)
    console.print_json(
        json.dumps(
            {
                "state": result.state,
                "reason": result.reason,
                "output": str(output.resolve()),
            }
        )
    )
    if result.state in {SafetyState.REJECTED, SafetyState.NEEDS_ATTENTION}:
        raise typer.Exit(code=2)


@system_opt_app.command("run")
def run_linux_system_optimization(
    manifest_path: Path = typer.Option(..., "--manifest", exists=True, dir_okay=False),
    policy_path: Path = typer.Option(..., "--policy", exists=True, dir_okay=False),
    capability_domains_path: Path = typer.Option(
        ..., "--capability-domains", exists=True, dir_okay=False
    ),
    authorized_domains_path: Path = typer.Option(
        ..., "--authorized-domains", exists=True, dir_okay=False
    ),
    baseline_parameters_path: Path = typer.Option(
        ..., "--baseline-parameters", exists=True, dir_okay=False
    ),
    measurement_command_path: Path = typer.Option(
        ..., "--measurement-command", exists=True, dir_okay=False
    ),
    diagnostic_reference_path: Path | None = typer.Option(
        None, "--diagnostic-reference", exists=True, dir_okay=False
    ),
    target_id: str = typer.Option(..., "--target-id"),
    owner_id: str = typer.Option(..., "--owner-id"),
    lease_root: Path = typer.Option(..., "--lease-root", file_okay=False),
    lease_ttl_seconds: float = typer.Option(..., "--lease-ttl-seconds", min=1),
    allow_executable: list[str] = typer.Option(..., "--allow-executable"),
    writable_root: list[Path] = typer.Option(..., "--writable-root", file_okay=False),
    state_evidence_path: Path = typer.Option(..., "--state-evidence", exists=True, dir_okay=False),
    reconciliation_evidence_path: Path | None = typer.Option(
        None, "--reconciliation-evidence", exists=True, dir_okay=False
    ),
    enable_real: bool = typer.Option(False, "--enable-real"),
    confirmation: str = typer.Option("", "--confirmation"),
    output: Path = typer.Option(..., "--output", dir_okay=False),
) -> None:
    _require_linux_confirmation(enable_real, confirmation)
    manifest = parse_config_manifest_yaml(manifest_path.read_text(encoding="utf-8"))
    policy = parse_optimization_policy_yaml(policy_path.read_text(encoding="utf-8"))
    capability_domains = TypeAdapter(list[DomainEvidence]).validate_python(
        _read_json(capability_domains_path)
    )
    authorized_domains = TypeAdapter(list[AuthorizedDomain]).validate_python(
        _read_json(authorized_domains_path)
    )
    capability_by_id = {domain.item_id: domain for domain in capability_domains}
    authorization_by_id = {domain.item_id: domain for domain in authorized_domains}
    domains = {}
    for item in manifest.items:
        if item.id not in capability_by_id or item.id not in authorization_by_id:
            continue
        resolved = resolve_domain(item, capability_by_id[item.id], authorization_by_id[item.id])
        domains[item.parameter_id] = resolved
    baseline = _read_json(baseline_parameters_path)
    if not isinstance(baseline, dict):
        raise typer.BadParameter("baseline parameters must be a JSON object")
    measurement_spec = MeasurementCommandSpec.model_validate(_read_json(measurement_command_path))
    minimum_lease = policy.search.wall_time_seconds + measurement_spec.timeout_seconds
    if lease_ttl_seconds <= minimum_lease:
        raise typer.BadParameter(
            "lease TTL must exceed search wall-time plus one measurement timeout"
        )
    if measurement_spec.argv[0] not in set(allow_executable):
        raise typer.BadParameter("measurement executable is not allowlisted")
    backend = _local_backend(
        manifest,
        target_id=target_id,
        allowed_executables=allow_executable,
        writable_roots=writable_root,
    )
    state_evidence = _load_state_evidence(state_evidence_path)
    pinned_items, ownership_unknown_items = state_evidence.safety_constraints(
        manifest,
        target_id=target_id,
        actor_id=owner_id,
        environment_digest=_current_environment_digest(),
    )
    policy = policy.model_copy(deep=True)
    policy.safety.pinned_items = sorted(set(policy.safety.pinned_items) | pinned_items)
    policy.safety.ownership_unknown_items = sorted(
        set(policy.safety.ownership_unknown_items) | ownership_unknown_items
    )
    runner = SubprocessCommandRunner(
        allowed_executables=set(allow_executable),
        writable_file_roots=writable_root,
    )
    measure = CommandMeasurementAdapter(measurement_spec, runner)
    reference = (
        MeasurementBatch.model_validate(_read_json(diagnostic_reference_path))
        if diagnostic_reference_path is not None
        else None
    )
    if policy.mode == OptimizationMode.WORKLOAD and reference is None:
        raise typer.BadParameter("workload mode requires --diagnostic-reference")
    guard = FileTargetGuard(lease_root)
    lease = guard.acquire(
        target_id,
        owner_id,
        ttl_seconds=lease_ttl_seconds,
        now=datetime.now(UTC),
        reconciliation=_load_reconciliation(reconciliation_evidence_path),
    )
    try:
        result = SystemOptimizationEngine(
            policy,
            manifest,
            domains,
            backend,
            state_evidence_digest=state_evidence.digest,
        ).run(
            baseline_parameters=baseline,
            measure=measure,
            fencing_token=lease.fencing_token,
            diagnostic_reference=reference,
        )
        _write_json(output, result)
        if any(
            candidate.safety_state == SafetyState.NEEDS_ATTENTION for candidate in result.candidates
        ):
            guard.mark_needs_attention(
                target_id,
                reason="optimization candidate rollback needs attention",
                evidence_digest=result.digest,
                now=datetime.now(UTC),
            )
    finally:
        guard.release(lease)
    console.print_json(
        json.dumps(
            {
                "stop_reason": result.stop_reason,
                "stop_detail": result.stop_detail,
                "candidate_rounds": len(result.candidates),
                "measurement_attempts": result.attempt_count,
                "baseline_measurements": len(result.baseline_history),
                "recommended_candidate_id": result.recommended_candidate_id,
                "output": str(output.resolve()),
            }
        )
    )


@app.command("init")
def initialize() -> None:
    init_database()
    with session_scope() as session:
        seed_system(session)
    console.print("Looper metadata and built-in benchmark are ready.")


@benchmark_app.command("validate")
def validate_benchmark(path: Path) -> None:
    manifest, digest = load_and_validate_manifest(path)
    console.print_json(
        json.dumps(
            {
                "valid": True,
                "id": manifest["metadata"]["id"],
                "version": manifest["metadata"]["version"],
                "digest": digest,
            }
        )
    )


@adapter_app.command("apply")
def apply_adapter_command(manifest: Path, input_path: Path) -> None:
    console.print_json(json.dumps(load_and_apply_adapter(manifest, input_path)))


@evidence_app.command("verify")
def verify_evidence(path: Path) -> None:
    console.print_json(json.dumps(verify_evidence_bundle(path)))


@cloud_app.command("adopt")
def adopt_cloud(
    provider: str = typer.Argument(help="Provider: tencent, alibaba, volcengine, or baidu"),
    instance_id: str = typer.Argument(),
    region: str = typer.Option(..., "--region"),
    zone: str = typer.Option(..., "--zone"),
    name: str = typer.Option(..., "--name"),
    instance_type: str = typer.Option(..., "--instance-type"),
    image_id: str = typer.Option(..., "--image-id"),
    state: str = typer.Option("RUNNING", "--state"),
    cpu: int | None = typer.Option(None, "--cpu", min=1),
    memory_gib: float | None = typer.Option(None, "--memory-gib", min=0.25),
    private_ip: str | None = typer.Option(None, "--private-ip"),
    public_ip_present: bool = typer.Option(False, "--public-ip/--no-public-ip"),
    vpc_id: str | None = typer.Option(None, "--vpc-id"),
    subnet_id: str | None = typer.Option(None, "--subnet-id"),
    source: str = typer.Option("external-adoption", "--source"),
) -> None:
    init_database()
    try:
        with session_scope() as session:
            record = adopt_cloud_target(
                session,
                provider=provider,
                region=region,
                zone=zone,
                instance_id=instance_id,
                name=name,
                instance_type=instance_type,
                image_id=image_id,
                state=state,
                cpu=cpu,
                memory_gib=memory_gib,
                private_ip=private_ip,
                public_ip_present=public_ip_present,
                vpc_id=vpc_id,
                subnet_id=subnet_id,
                source=source,
            )
            target_id = record.id
    except ValueError as error:
        error_console.print(f"[red]cloud adoption error:[/red] {error}")
        raise typer.Exit(code=2) from error
    console.print(target_id)


@cloud_app.command("configure")
def configure_cloud(
    provider: str = typer.Argument(help="Provider: tencent or alibaba"),
    env_file: Path = typer.Option(Path(".env"), "--env-file"),
    max_hourly_amount: float = typer.Option(10.0, "--max-hourly-amount", min=0.01),
) -> None:
    try:
        fields = credential_fields(provider)
        values: dict[str, str] = {}
        for variable, label, required in fields:
            prompt = f"{provider.title()} {label}"
            values[variable] = typer.prompt(
                prompt,
                default=None if required else "",
                hide_input=True,
                show_default=False,
            )
        result = configure_cloud_purchase(
            provider,
            values,
            env_file=env_file,
            max_hourly_amount=Decimal(str(max_hourly_amount)),
        )
    except ValueError as error:
        error_console.print(f"[red]cloud configuration error:[/red] {error}")
        raise typer.Exit(code=2) from error

    console.print(f"Cloud purchase configuration written to {result.env_file}")
    console.print(f"Provider allowlisted: {result.provider}")
    console.print(f"Hourly spend cap: {result.max_hourly_amount}")
    console.print("Restart the Looper API, then enter this Operator token in the Web key control:")
    console.print(result.operator_token)


@source_app.command("list")
def list_sources(
    lock_path: Path = Path("third_party/sources.lock.yaml"),
) -> None:
    lock = load_source_lock(lock_path)
    rows = [
        {
            "id": item["id"],
            "license": item.get("license"),
            "status": item["inclusion_status"],
            "commit": item.get("commit"),
        }
        for item in lock["sources"]
    ]
    console.print_json(json.dumps(rows))


@source_app.command("resolve")
def resolve_source_command(
    source_id: str,
    lock_path: Path = Path("third_party/sources.lock.yaml"),
) -> None:
    try:
        result = resolve_source(lock_path, source_id)
    except SourcePolicyError as error:
        error_console.print(f"[red]source policy error:[/red] {error}")
        raise typer.Exit(code=2) from error
    console.print_json(json.dumps(result))


@source_app.command("fetch")
def fetch_source_command(
    source_id: str,
    lock_path: Path = Path("third_party/sources.lock.yaml"),
    cache_root: Path = Path(".looper/upstreams"),
) -> None:
    try:
        result = fetch_source(lock_path, source_id, cache_root)
    except SourcePolicyError as error:
        error_console.print(f"[red]source policy error:[/red] {error}")
        raise typer.Exit(code=2) from error
    console.print_json(json.dumps(result))


@demo_app.command("create")
def create_demo(name: str = "Compression Pareto study", start: bool = False) -> None:
    init_database()
    with session_scope() as session:
        seed_system(session)
        experiment = create_experiment(session, create_demo_request(name))
        if start:
            start_experiment(session, experiment)
        console.print(experiment.id)


@demo_app.command("start")
def start_demo(experiment_id: str) -> None:
    with session_scope() as session:
        experiment = session.scalar(
            select(ExperimentRecord).where(ExperimentRecord.id == experiment_id)
        )
        if experiment is None:
            raise typer.BadParameter("experiment does not exist")
        start_experiment(session, experiment)
        console.print(f"Queued {experiment.id}")


@demo_app.command("verified-loop")
def run_verified_demo(
    compression_level: int = typer.Option(1, "--compression-level", min=1, max=9),
    chunk_size: int = typer.Option(65536, "--chunk-size"),
    repeats: int = typer.Option(3, "--repeats", min=2, max=100),
    minimum_improvement: float = typer.Option(0.05, "--minimum-improvement", min=0),
    maximum_ratio_regression: float = typer.Option(0.15, "--maximum-ratio-regression", min=0),
    samples: int = typer.Option(12, "--samples", min=3, max=10000),
    size_kib: int = typer.Option(512, "--size-kib", min=128, max=65536),
    workspace: Path = typer.Option(Path(".looper/verified-action"), "--workspace", file_okay=False),
) -> None:
    """Run a real local test -> change -> retest -> keep/rollback loop."""

    try:
        result = run_verified_compression_loop(
            workspace,
            candidate={
                "compression_level": compression_level,
                "chunk_size": chunk_size,
            },
            policy=VerificationPolicy(
                repeats=repeats,
                minimum_improvement_ratio=minimum_improvement,
                maximum_secondary_regression_ratio=maximum_ratio_regression,
                confidence_level=0.95,
                bootstrap_resamples=1000,
                random_seed=20260822,
            ),
            samples=samples,
            size_kib=size_kib,
        )
    except (OSError, RuntimeError, ValueError) as error:
        error_console.print(f"[red]verified action loop failed:[/red] {error}")
        raise typer.Exit(code=2) from error
    console.print_json(json.dumps(result))
    if result["decision"] == "failed":
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
