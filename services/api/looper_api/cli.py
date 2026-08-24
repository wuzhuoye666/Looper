from __future__ import annotations

import json
import os
import platform
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from looper_core.action_loop import VerificationPolicy
from looper_core.adapters import load_and_apply_adapter
from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.manifest import load_and_validate_manifest
from looper_core.system_opt.collector import BuiltinLinuxGuestCollector
from looper_core.system_opt.component import ComponentOptimizer
from looper_core.system_opt.config_manifest import (
    ConfigItem,
    ConfigManifest,
    parse_config_manifest_yaml,
)
from looper_core.system_opt.demo import (
    SyntheticMeasurementAdapter,
    build_demo_manifest,
    build_demo_policy,
    resolve_demo_domains,
    run_full_demo,
)
from looper_core.system_opt.domain import (
    AuthorizedDomain,
    DomainEvidence,
    resolve_domain,
)
from looper_core.system_opt.dynamic_adapters import (
    BusinessRetestPlanner,
    FileHypothesisProposals,
    FileLoadIdentity,
    FileO0Source,
    FileRetestSource,
    SafetyBackedIntervention,
    SessionLayout,
    load_business_policy,
    load_hypothesis_proposals,
    load_o1_collection_plans,
    load_workload_contract,
)
from looper_core.system_opt.dynamic_collection import (
    o1_live_source,
    o2_component_probe,
    persist_dynamic_collection_evidence,
)
from looper_core.system_opt.dynamic_loop import run_dynamic_phase
from looper_core.system_opt.engine import EngineLoopConfig, run_engine_loop
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
from looper_core.system_opt.negative_cache import NegativeCache
from looper_core.system_opt.phase_gate import DynamicPhaseGateContract
from looper_core.system_opt.policy import (
    OptimizationMode,
    parse_optimization_policy_yaml,
)
from looper_core.system_opt.pressure import (
    PhasedPressureMeasurementAdapter,
    StandardPressureProtocol,
    calibrate_cv_acceptance_limit,
    parse_standard_pressure_protocol_yaml,
    validate_pressure_policy,
)
from looper_core.system_opt.result_vector import PromotionContract
from looper_core.system_opt.rollback.regression import (
    RegressionRecoveryOutcome,
    RegressionRecoveryRequest,
    RegressionRecoveryStatus,
    execute_regression_recovery,
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
from pydantic import StringConstraints, TypeAdapter, model_validator
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


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=False)
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _normalized_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _ensure_distinct_paths(named_paths: dict[str, Path]) -> dict[str, Path]:
    normalized = {name: _normalized_path(path) for name, path in named_paths.items()}
    seen: dict[Path, str] = {}
    for name, path in normalized.items():
        if path in seen:
            raise typer.BadParameter(f"path collision between {seen[path]} and {name}: {path}")
        seen[path] = name
    evidence_root = normalized.get("evidence-root")
    if evidence_root is not None:
        for name in ("request", "manifest", "state-evidence", "initial-state", "output"):
            path = normalized.get(name)
            if path is not None and evidence_root in path.parents:
                raise typer.BadParameter(f"input path {name} is inside evidence root: {path}")
    return normalized


REGRESSION_RECOVERY_EVIDENCE_INDEX_SCHEMA = (
    "looper.regression-recovery-evidence-index/v1alpha1"
)
_SHA256_DIGEST = r"^sha256:[0-9a-f]{64}$"
_Digest = Annotated[str, StringConstraints(pattern=_SHA256_DIGEST)]
_EvidenceFilename = Annotated[
    str,
    StringConstraints(
        pattern=r"^(request|outcome|rollback)-[0-9a-f]{64}\.json$"
    ),
]


class RegressionRecoveryEvidenceIndex(StrictModel):
    """Fixed replay index for one fully published L6c evidence graph."""

    schema_version: Literal[REGRESSION_RECOVERY_EVIDENCE_INDEX_SCHEMA] = (
        REGRESSION_RECOVERY_EVIDENCE_INDEX_SCHEMA
    )
    request_digest: _Digest
    outcome_digest: _Digest
    rollback_record_digest: _Digest | None = None
    request_path: _EvidenceFilename
    outcome_path: _EvidenceFilename
    rollback_record_path: _EvidenceFilename | None = None

    @model_validator(mode="after")
    def validate_content_addressed_paths(self) -> RegressionRecoveryEvidenceIndex:
        expected_request = f"request-{self.request_digest.removeprefix('sha256:')}.json"
        expected_outcome = f"outcome-{self.outcome_digest.removeprefix('sha256:')}.json"
        if self.request_path != expected_request:
            raise ValueError("request evidence path is not bound to its digest")
        if self.outcome_path != expected_outcome:
            raise ValueError("outcome evidence path is not bound to its digest")
        if (self.rollback_record_digest is None) != (
            self.rollback_record_path is None
        ):
            raise ValueError("rollback digest and path must be present together")
        if self.rollback_record_digest is not None:
            expected_rollback = (
                "rollback-"
                f"{self.rollback_record_digest.removeprefix('sha256:')}.json"
            )
            if self.rollback_record_path != expected_rollback:
                raise ValueError("rollback evidence path is not bound to its digest")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class RegressionRecoveryEvidenceGraph(StrictModel):
    """In-memory graph validated completely before any evidence file is created."""

    request: RegressionRecoveryRequest
    outcome: RegressionRecoveryOutcome
    index: RegressionRecoveryEvidenceIndex

    @model_validator(mode="after")
    def validate_associations(self) -> RegressionRecoveryEvidenceGraph:
        request = self.request
        outcome = self.outcome
        rollback = outcome.rollback_record
        if outcome.request_digest != request.digest:
            raise ValueError("outcome request digest does not match request")
        if self.index.request_digest != request.digest:
            raise ValueError("index request digest does not match request")
        if self.index.outcome_digest != outcome.digest:
            raise ValueError("index outcome digest does not match outcome")
        rollback_digest = rollback.digest if rollback is not None else None
        if self.index.rollback_record_digest != rollback_digest:
            raise ValueError("index rollback digest does not match outcome")
        execution = outcome.execution_evidence
        if execution is not None and execution.request_digest != request.digest:
            raise ValueError("execution evidence does not match request")
        if rollback is not None:
            if execution is None:
                raise ValueError("rollback evidence requires execution evidence")
            checkpoint = request.checkpoint
            if rollback.target_id != checkpoint.target_id:
                raise ValueError("rollback target does not match request checkpoint")
            if rollback.item_ids != sorted(checkpoint.snapshot.entries):
                raise ValueError("rollback items do not match checkpoint snapshot")
            if rollback.baseline_snapshot_digest != checkpoint.snapshot.digest:
                raise ValueError("rollback baseline does not match checkpoint snapshot")
            if request.digest not in rollback.evidence_digests:
                raise ValueError("rollback evidence does not reference request")
            if execution.digest not in rollback.evidence_digests:
                raise ValueError("rollback evidence does not reference execution")
            if rollback.checkpoint_digest != checkpoint.digest:
                raise ValueError("rollback checkpoint does not match request")
            if rollback.regression_vector_digest != request.current_vector.digest:
                raise ValueError("rollback vector does not match request")
            if rollback.regression_threshold != request.regression_threshold:
                raise ValueError("rollback threshold does not match request")
            safety_result = execution.safety_result
            final_snapshot = (
                safety_result.final_snapshot if safety_result is not None else None
            )
            final_snapshot_digest = (
                final_snapshot.digest if final_snapshot is not None else None
            )
            if rollback.final_snapshot_digest != final_snapshot_digest:
                raise ValueError("rollback final snapshot does not match execution")
            restoration = execution.restoration
            if restoration is not None:
                if restoration.baseline_snapshot_digest != checkpoint.snapshot.digest:
                    raise ValueError("restoration baseline does not match checkpoint")
                if restoration.actual_snapshot_digest != final_snapshot_digest:
                    raise ValueError("restoration snapshot does not match execution")
        return self


def _build_regression_recovery_evidence_graph(
    request: RegressionRecoveryRequest,
    outcome: RegressionRecoveryOutcome,
) -> RegressionRecoveryEvidenceGraph:
    rollback = outcome.rollback_record
    rollback_digest = rollback.digest if rollback is not None else None
    index = RegressionRecoveryEvidenceIndex(
        request_digest=request.digest,
        outcome_digest=outcome.digest,
        rollback_record_digest=rollback_digest,
        request_path=f"request-{request.digest.removeprefix('sha256:')}.json",
        outcome_path=f"outcome-{outcome.digest.removeprefix('sha256:')}.json",
        rollback_record_path=(
            f"rollback-{rollback_digest.removeprefix('sha256:')}.json"
            if rollback_digest is not None
            else None
        ),
    )
    return RegressionRecoveryEvidenceGraph(
        request=request,
        outcome=outcome,
        index=index,
    )


def _persist_regression_recovery_evidence_graph(
    evidence_dir: Path,
    graph: RegressionRecoveryEvidenceGraph,
) -> None:
    """Publish digest files atomically and the fixed index last.

    A failed publication may retain unindexed forensic files, but it cannot
    publish a new index that presents a partial graph as complete.
    """

    index = graph.index
    _write_json_atomic(evidence_dir / index.request_path, graph.request)
    _write_json_atomic(evidence_dir / index.outcome_path, graph.outcome)
    rollback = graph.outcome.rollback_record
    if rollback is not None:
        assert index.rollback_record_path is not None
        _write_json_atomic(evidence_dir / index.rollback_record_path, rollback)
    _write_json_atomic(
        evidence_dir / "regression-recovery-evidence-index.json",
        index,
    )


def _mark_regression_attention(
    guard: FileTargetGuard,
    *,
    target_id: str,
    reason: str,
    evidence_digest: str,
    primary_error: Exception | None = None,
) -> None:
    try:
        guard.mark_needs_attention(
            target_id,
            reason=reason,
            evidence_digest=evidence_digest,
            now=datetime.now(UTC),
        )
    except Exception as attention_error:
        combined = f"{reason}; attention write failed: {attention_error}"
        if primary_error is not None:
            raise RuntimeError(combined) from primary_error
        raise RuntimeError(combined) from attention_error


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


def _decoupled_pressure_measure(
    protocol: StandardPressureProtocol,
    runner: SubprocessCommandRunner,
    *,
    target_id: str,
    collection_enabled: bool,
    collector: Any | None = None,
) -> Callable[[int], MeasurementBatch] | None:
    """Route collection-decoupled pressure protocols to an L4 windowed collector.

    Returns ``None`` for legacy protocols (no ``collection`` contract) so callers
    keep the existing PhasedPressureMeasurementAdapter path unchanged. The
    decoupled symbols are imported lazily so this CLI stays importable on trees
    where the PKG-B collection contract has not landed yet.
    """

    contract = getattr(protocol, "collection", None)
    if contract is None:
        return None
    if not collection_enabled:
        raise typer.BadParameter(
            "collection-decoupled protocols require collection to be enabled: "
            "a disabled collection run emits no MeasurementBatch"
        )
    from looper_core.system_opt.collector import BuiltinLinuxGuestCollector
    from looper_core.system_opt.pressure import PhasedPressureCollectionAdapter

    selected = collector if collector is not None else BuiltinLinuxGuestCollector()
    if contract.collector_id != selected.collector_id:
        raise typer.BadParameter(
            f"collection contract selects collector '{contract.collector_id}' but "
            f"only '{selected.collector_id}' is available to this CLI"
        )
    if not hasattr(selected, "begin_collection"):
        raise typer.BadParameter(
            f"collector '{selected.collector_id}' does not implement measure-window "
            "collection sessions; windowed production collection is pending PKG-B"
        )
    adapter = PhasedPressureCollectionAdapter(
        protocol,
        runner,
        collector=selected,
        target_id=target_id,
        environment_digest=_current_environment_digest(),
        collection_enabled=True,
    )

    def measure(repeats: int) -> MeasurementBatch:
        envelope = adapter(repeats).envelope
        if envelope is None:
            raise RuntimeError("enabled pressure collection produced no measurement envelope")
        return envelope.measurement_batch

    return measure


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
    measurement_command_path: Path | None = typer.Option(
        None, "--measurement-command", exists=True, dir_okay=False
    ),
    pressure_protocol_path: Path | None = typer.Option(
        None, "--pressure-protocol", exists=True, dir_okay=False
    ),
    collection_enabled: bool = typer.Option(
        True,
        "--collection-enabled/--no-collection-enabled",
        help="enable L4 windowed collection for collection-decoupled pressure protocols",
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
    if (measurement_command_path is None) == (pressure_protocol_path is None):
        raise typer.BadParameter(
            "provide exactly one of --measurement-command or --pressure-protocol"
        )
    pressure_protocol = (
        parse_standard_pressure_protocol_yaml(
            pressure_protocol_path.read_text(encoding="utf-8")
        )
        if pressure_protocol_path is not None
        else None
    )
    if pressure_protocol is not None:
        try:
            validate_pressure_policy(pressure_protocol, policy)
        except ValueError as error:
            raise typer.BadParameter(str(error)) from error
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
    measurement_spec = (
        MeasurementCommandSpec.model_validate(_read_json(measurement_command_path))
        if measurement_command_path is not None
        else None
    )
    measurement_timeout = (
        measurement_spec.timeout_seconds
        if measurement_spec is not None
        else sum(phase.command.timeout_seconds for phase in pressure_protocol.phases)
    )
    minimum_lease = policy.search.wall_time_seconds + measurement_timeout
    if lease_ttl_seconds <= minimum_lease:
        raise typer.BadParameter(
            "lease TTL must exceed search wall-time plus one measurement timeout"
        )
    allowed_executables = set(allow_executable)
    if measurement_spec is not None and measurement_spec.argv[0] not in allowed_executables:
        raise typer.BadParameter("measurement executable is not allowlisted")
    if pressure_protocol is not None:
        missing_executables = sorted(
            set(pressure_protocol.required_executables) - allowed_executables
        )
        if missing_executables:
            raise typer.BadParameter(
                f"pressure executables are not allowlisted: {missing_executables}"
            )
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
    if measurement_spec is not None:
        measure: Callable[[int], MeasurementBatch] = CommandMeasurementAdapter(
            measurement_spec, runner
        )
    else:
        assert pressure_protocol is not None
        decoupled = _decoupled_pressure_measure(
            pressure_protocol,
            runner,
            target_id=target_id,
            collection_enabled=collection_enabled,
        )
        measure = decoupled if decoupled is not None else PhasedPressureMeasurementAdapter(
            pressure_protocol, runner
        )
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


@system_opt_app.command("calibrate-pressure")
def calibrate_linux_pressure(
    pressure_protocol_path: Path = typer.Option(
        ..., "--pressure-protocol", exists=True, dir_okay=False
    ),
    repeats: int = typer.Option(..., "--repeats", min=2),
    target_id: str = typer.Option(..., "--target-id"),
    owner_id: str = typer.Option(..., "--owner-id"),
    lease_root: Path = typer.Option(..., "--lease-root", file_okay=False),
    lease_ttl_seconds: float = typer.Option(..., "--lease-ttl-seconds", min=1),
    allow_executable: list[str] = typer.Option(..., "--allow-executable"),
    writable_root: list[Path] = typer.Option(..., "--writable-root", file_okay=False),
    collection_enabled: bool = typer.Option(
        True,
        "--collection-enabled/--no-collection-enabled",
        help="enable L4 windowed collection for collection-decoupled pressure protocols",
    ),
    enable_real: bool = typer.Option(False, "--enable-real"),
    confirmation: str = typer.Option("", "--confirmation"),
    output: Path = typer.Option(..., "--output", dir_okay=False),
) -> None:
    _require_linux_confirmation(enable_real, confirmation)
    protocol = parse_standard_pressure_protocol_yaml(
        pressure_protocol_path.read_text(encoding="utf-8")
    )
    allowed_executables = set(allow_executable)
    missing_executables = sorted(set(protocol.required_executables) - allowed_executables)
    if missing_executables:
        raise typer.BadParameter(
            f"pressure executables are not allowlisted: {missing_executables}"
        )
    maximum_runtime = sum(phase.command.timeout_seconds for phase in protocol.phases)
    if lease_ttl_seconds <= maximum_runtime:
        raise typer.BadParameter("lease TTL must exceed the sum of pressure phase timeouts")
    runner = SubprocessCommandRunner(
        allowed_executables=allowed_executables,
        writable_file_roots=writable_root,
    )
    guard = FileTargetGuard(lease_root)
    lease = guard.acquire(
        target_id,
        owner_id,
        ttl_seconds=lease_ttl_seconds,
        now=datetime.now(UTC),
        reconciliation=None,
    )
    try:
        decoupled = _decoupled_pressure_measure(
            protocol,
            runner,
            target_id=target_id,
            collection_enabled=collection_enabled,
        )
        measure = (
            decoupled
            if decoupled is not None
            else PhasedPressureMeasurementAdapter(protocol, runner)
        )
        batch = measure(repeats)
        _write_json(output, batch)
    finally:
        guard.release(lease)
    console.print_json(
        json.dumps(
            {
                "protocol_id": protocol.id,
                "protocol_digest": protocol.digest,
                "component": protocol.component,
                "stability": (
                    batch.stability_evidence.model_dump(mode="json")
                    if batch.stability_evidence is not None
                    else None
                ),
                "output": str(output.resolve()),
            }
        )
    )


@system_opt_app.command("derive-pressure-gate")
def derive_pressure_gate(
    measurement_batch_path: Path = typer.Option(
        ..., "--measurement-batch", exists=True, dir_okay=False
    ),
    metric_id: str = typer.Option(..., "--metric-id"),
    confidence_level: float = typer.Option(..., "--confidence-level", min=0.500001, max=0.999999),
    bootstrap_resamples: int = typer.Option(
        ..., "--bootstrap-resamples", min=100, max=100000
    ),
    random_seed: int = typer.Option(..., "--random-seed", min=0),
    target_scope: str = typer.Option(..., "--target-scope"),
    portability: str = typer.Option(..., "--portability"),
    output: Path = typer.Option(..., "--output", dir_okay=False),
) -> None:
    """Derive an explicit target-local CV gate from a frozen calibration batch."""

    batch = MeasurementBatch.model_validate(_read_json(measurement_batch_path))
    try:
        evidence = calibrate_cv_acceptance_limit(
            batch,
            metric_id,
            confidence_level=confidence_level,
            bootstrap_resamples=bootstrap_resamples,
            random_seed=random_seed,
            target_scope=target_scope,
            portability=portability,
        )
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    _write_json(output, evidence)
    console.print_json(
        json.dumps(
            {
                "metric_id": evidence.metric_id,
                "acceptance_limit": evidence.acceptance_limit,
                "calibration_digest": evidence.digest,
                "output": str(output.resolve()),
            }
        )
    )


@system_opt_app.command("dynamic-run")
def run_dynamic_phase_session(
    session_dir: Path = typer.Option(..., "--session", exists=True, file_okay=False),
    manifest_path: Path = typer.Option(..., "--manifest", exists=True, dir_okay=False),
    state_evidence_path: Path = typer.Option(
        ..., "--state-evidence", exists=True, dir_okay=False
    ),
    backend_kind: str = typer.Option(..., "--backend"),
    initial_state: Path | None = typer.Option(
        None, "--initial-state", exists=True, dir_okay=False
    ),
    target_id: str = typer.Option(..., "--target-id"),
    owner_id: str = typer.Option(..., "--owner-id"),
    lease_root: Path = typer.Option(..., "--lease-root", file_okay=False),
    lease_ttl_seconds: float = typer.Option(..., "--lease-ttl-seconds", min=1),
    allow_executable: list[str] | None = typer.Option(None, "--allow-executable"),
    writable_root: list[Path] = typer.Option([], "--writable-root", file_okay=False),
    max_windows: int = typer.Option(..., "--max-windows", min=1),
    probe_top_k: int = typer.Option(..., "--probe-top-k", min=1),
    verification_window_count: int = typer.Option(..., "--verification-windows", min=0),
    o1_plans_path: Path | None = typer.Option(
        None, "--o1-plans", exists=True, dir_okay=False
    ),
    o1_window_seconds: float | None = typer.Option(
        None, "--o1-window-seconds", min=0.001
    ),
    o2_window_seconds: float | None = typer.Option(
        None, "--o2-window-seconds", min=0.001
    ),
    o2_source: str = typer.Option(
        "window-digest", "--o2-source", help="probe evidence source: window-digest | live"
    ),
    enable_real: bool = typer.Option(False, "--enable-real"),
    confirmation: str = typer.Option("", "--confirmation"),
    output: Path = typer.Option(..., "--output", dir_okay=False),
) -> None:
    """Run one dynamic phase over an externally loaded session directory.

    SO-D020: the load is external; this command only reads ``windows/`` and
    writes ``control/`` inside the session directory. The phase always ends by
    restoring the phase-start configuration through the L1 safety path, so the
    machine returns to its starting state regardless of promotion outcome.
    """

    if backend_kind == "local-linux":
        _require_linux_confirmation(enable_real, confirmation)
    elif backend_kind != "simulated":
        raise typer.BadParameter("backend must be simulated or local-linux")
    if backend_kind == "simulated" and initial_state is None:
        raise typer.BadParameter("simulated backend requires --initial-state")

    layout = SessionLayout(session_dir)
    contract = load_workload_contract(layout)
    gate_contract = DynamicPhaseGateContract.model_validate_json(
        layout.gate_contract.read_text(encoding="utf-8")
    )
    promotion_contract = PromotionContract.model_validate_json(
        layout.promotion_contract.read_text(encoding="utf-8")
    )
    business_policy = load_business_policy(layout.business_policy)
    baseline_batch = MeasurementBatch.model_validate_json(
        layout.baseline_batch.read_text(encoding="utf-8")
    )
    proposals = load_hypothesis_proposals(layout.hypothesis_proposals)
    if lease_ttl_seconds <= gate_contract.budget.wall_clock_seconds:
        raise typer.BadParameter(
            "lease TTL must exceed the gate contract wall-clock budget"
        )
    if o2_source not in {"window-digest", "live"}:
        raise typer.BadParameter("--o2-source must be window-digest or live")
    o1_plans = None
    if o1_plans_path is not None:
        if o1_window_seconds is None:
            raise typer.BadParameter("--o1-plans requires --o1-window-seconds")
        if o2_source == "live" and o2_window_seconds is None:
            raise typer.BadParameter("--o2-source live requires --o2-window-seconds")
        if backend_kind != "local-linux":
            raise typer.BadParameter(
                "live O1/O2 collection requires the local-linux backend"
            )
        try:
            o1_plans = load_o1_collection_plans(
                o1_plans_path, environment_digest=_current_environment_digest()
            )
        except ValueError as error:
            raise typer.BadParameter(str(error)) from error

    manifest = parse_config_manifest_yaml(manifest_path.read_text(encoding="utf-8"))
    if backend_kind == "simulated":
        payload = _read_json(initial_state)
        if not isinstance(payload, dict):
            raise typer.BadParameter("initial state must be a JSON object")
        backend = SimulatedBackend(payload, target_id=target_id)
    else:
        backend = _local_backend(
            manifest,
            target_id=target_id,
            allowed_executables=allow_executable or [],
            writable_roots=list(writable_root),
        )

    state_evidence = _load_state_evidence(state_evidence_path)
    pinned_items, ownership_unknown_items = state_evidence.safety_constraints(
        manifest,
        target_id=target_id,
        actor_id=owner_id,
        environment_digest=_current_environment_digest(),
    )
    safety_policy = SafetyPolicy(
        allow_keep=True,
        pinned_items=sorted(pinned_items),
        ownership_unknown_items=sorted(ownership_unknown_items),
    )
    controller = SafetyController(safety_policy)

    guard = FileTargetGuard(lease_root)
    lease = guard.acquire(
        target_id,
        owner_id,
        ttl_seconds=lease_ttl_seconds,
        now=datetime.now(UTC),
        reconciliation=_load_reconciliation(None),
    )
    try:
        changeable_ids: set[str] = set()
        for proposal in proposals.proposals:
            for parameter_id in proposal.change:
                changeable_ids.add(manifest.item_for_parameter(parameter_id).id)
        phase_items = manifest.ordered_items(changeable_ids)
        phase_snapshot = backend.snapshot(phase_items, fencing_token=lease.fencing_token)
        if not phase_snapshot.complete:
            raise typer.BadParameter(
                "phase-start snapshot is incomplete; refusing to start a dynamic "
                "phase whose restoration cannot be guaranteed"
            )

        planner = BusinessRetestPlanner(
            contract=contract,
            policy=business_policy,
            baseline_batch=baseline_batch,
            layout=layout,
        )
        intervention = SafetyBackedIntervention(
            controller=controller,
            manifest=manifest,
            backend=backend,
            fencing_token=lease.fencing_token,
            proposals=proposals.by_id(),
            planner=planner,
            layout=layout,
        )
        live_o1 = None
        live_o2 = None
        if o1_plans is not None:
            collectors = {
                plan.component: BuiltinLinuxGuestCollector() for plan in o1_plans
            }
            live_o1 = o1_live_source(
                plans=o1_plans,
                collectors=collectors,
                window_seconds=o1_window_seconds,
            )
            if o2_source == "live":
                live_o2 = o2_component_probe(
                    plans=o1_plans,
                    collectors=collectors,
                    window_seconds=o2_window_seconds,
                )
        # Unavailable live sources degrade explicitly: O1 stays off, O2 falls
        # back to the window-digest placeholder; the summary reports which
        # mode actually ran, never silently pretending live collection.
        o2_callback = (
            live_o2
            if live_o2 is not None
            else (lambda hypothesis, window: window.digest)
        )
        restoration_values: dict[str, Any] | None = None
        run = None
        run_error: Exception | None = None
        restoration_error: Exception | None = None
        restoration_audit_error: Exception | None = None
        evidence_error: Exception | None = None
        restoration = None
        try:
            restoration_values = {
                manifest.item(item_id).parameter_id: entry.value
                for item_id, entry in phase_snapshot.entries.items()
            }
            run = run_dynamic_phase(
                contract=contract,
                gate_contract=gate_contract,
                promotion_contract=promotion_contract,
                environment_digest=_current_environment_digest(),
                max_windows=max_windows,
                probe_top_k=probe_top_k,
                load_identity=FileLoadIdentity(layout, business_policy),
                o0_source=FileO0Source(layout, business_policy),
                hypothesis_source=FileHypothesisProposals(proposals),
                clock=lambda: datetime.now(UTC),
                o1_source=live_o1,
                component_probe=o2_callback,
                intervention=intervention,
                retest=FileRetestSource(planner, layout),
                verification_window_count=verification_window_count,
            )
        except Exception as error:
            run_error = error
        finally:
            if restoration_values is None:
                restoration_error = RuntimeError(
                    "phase restoration values could not be constructed from the complete "
                    "phase-start snapshot"
                )
            else:
                try:
                    restoration = controller.execute(
                        manifest,
                        restoration_values,
                        backend,
                        fencing_token=lease.fencing_token,
                        keep=True,
                        keep_authorized=True,
                    )
                except Exception as error:
                    restoration_error = error
                else:
                    if restoration.state is not SafetyState.KEPT:
                        restoration_error = RuntimeError(
                            f"phase restoration returned {restoration.state.value}: "
                            f"{restoration.reason}"
                        )
                    try:
                        layout.control.mkdir(parents=True, exist_ok=True)
                        (layout.control / "phase-restoration.json").write_text(
                            json.dumps(restoration.model_dump(mode="json"), indent=2),
                            encoding="utf-8",
                        )
                    except Exception as error:
                        restoration_audit_error = error

            if restoration_error is not None:
                evidence_digest = run.digest if run is not None else phase_snapshot.digest
                original = (
                    f"original dynamic phase error: {type(run_error).__name__}: {run_error}"
                    if run_error is not None
                    else "dynamic phase completed without an exception"
                )
                audit = (
                    f"; phase restoration audit error: "
                    f"{type(restoration_audit_error).__name__}: {restoration_audit_error}"
                    if restoration_audit_error is not None
                    else ""
                )
                guard.mark_needs_attention(
                    target_id,
                    reason=(
                        f"dynamic phase restoration failed; {original}; "
                        f"restoration error: {type(restoration_error).__name__}: "
                        f"{restoration_error}{audit}"
                    ),
                    evidence_digest=evidence_digest,
                    now=datetime.now(UTC),
                )

            try:
                persist_dynamic_collection_evidence(
                    layout.control,
                    o1_source=live_o1,
                    o2_probe=live_o2,
                )
            except Exception as error:
                evidence_error = error

        if run_error is not None:
            detail = f"dynamic phase failed: {type(run_error).__name__}: {run_error}"
            if restoration_error is not None:
                detail += (
                    f"; phase restoration failed: "
                    f"{type(restoration_error).__name__}: {restoration_error}"
                )
            if restoration_audit_error is not None:
                detail += (
                    f"; phase restoration audit failed: "
                    f"{type(restoration_audit_error).__name__}: "
                    f"{restoration_audit_error}"
                )
            if evidence_error is not None:
                detail += (
                    f"; evidence persistence failed: "
                    f"{type(evidence_error).__name__}: {evidence_error}"
                )
            raise RuntimeError(detail) from run_error
        if restoration_error is not None:
            detail = (
                f"phase restoration failed: {type(restoration_error).__name__}: "
                f"{restoration_error}"
            )
            if restoration_audit_error is not None:
                detail += (
                    f"; phase restoration audit failed: "
                    f"{type(restoration_audit_error).__name__}: "
                    f"{restoration_audit_error}"
                )
            if evidence_error is not None:
                detail += (
                    f"; evidence persistence failed: "
                    f"{type(evidence_error).__name__}: {evidence_error}"
                )
            raise typer.BadParameter(detail) from restoration_error
        if restoration_audit_error is not None:
            detail = (
                f"phase restoration audit failed: "
                f"{type(restoration_audit_error).__name__}: {restoration_audit_error}"
            )
            if evidence_error is not None:
                detail += (
                    f"; evidence persistence failed: "
                    f"{type(evidence_error).__name__}: {evidence_error}"
                )
            raise RuntimeError(detail) from restoration_audit_error
        if evidence_error is not None:
            raise RuntimeError(
                f"dynamic collection evidence persistence failed: "
                f"{type(evidence_error).__name__}: {evidence_error}"
            ) from evidence_error
        assert run is not None
        _write_json(output, run.model_dump(mode="json"))
    finally:
        guard.release(lease)
    console.print_json(
        json.dumps(
            {
                "windows": len(run.windows),
                "stop": run.stop_gate_decision.stop,
                "stop_class": (
                    run.stop_gate_decision.stop_class.value
                    if run.stop_gate_decision.stop_class
                    else None
                ),
                "stop_reason": run.stop_gate_decision.reason,
                "promotion": (
                    {
                        "candidate_id": run.promotion.candidate_id,
                        "promoted": run.promotion.promoted,
                    }
                    if run.promotion
                    else None
                ),
                "run_digest": run.digest,
                "o1_live_collection": live_o1 is not None,
                "o2_probe_source": "live" if live_o2 is not None else o2_source,
                "output": str(output.resolve()),
            }
        )
    )


@system_opt_app.command("regression-recovery")
def run_regression_recovery(
    request_path: Path = typer.Option(..., "--request", exists=True, dir_okay=False),
    manifest_path: Path = typer.Option(..., "--manifest", exists=True, dir_okay=False),
    state_evidence_path: Path = typer.Option(..., "--state-evidence", exists=True, dir_okay=False),
    backend_kind: str = typer.Option(..., "--backend"),
    initial_state: Path | None = typer.Option(None, "--initial-state", exists=True, dir_okay=False),
    target_id: str = typer.Option(..., "--target-id"),
    owner_id: str = typer.Option(..., "--owner-id"),
    lease_root: Path = typer.Option(..., "--lease-root", file_okay=False),
    lease_ttl_seconds: float = typer.Option(..., "--lease-ttl-seconds", min=1),
    allow_executable: list[str] | None = typer.Option(None, "--allow-executable"),
    writable_root: list[Path] = typer.Option([], "--writable-root", file_okay=False),
    enable_real: bool = typer.Option(False, "--enable-real"),
    confirmation: str = typer.Option("", "--confirmation"),
    evidence_dir: Path = typer.Option(..., "--evidence-dir", file_okay=False),
    output: Path = typer.Option(..., "--output", dir_okay=False),
) -> None:
    """Run explicit L6c regression recovery from a versioned request."""
    request = RegressionRecoveryRequest.model_validate(_read_json(request_path))
    if request.checkpoint.target_id != target_id:
        raise typer.BadParameter("request checkpoint target does not match --target-id")
    if backend_kind == "local-linux":
        _require_linux_confirmation(enable_real, confirmation)
    elif backend_kind != "simulated":
        raise typer.BadParameter("backend must be simulated or local-linux")
    if backend_kind == "simulated" and initial_state is None:
        raise typer.BadParameter("simulated backend requires --initial-state")
    _ensure_distinct_paths(
        {
            "request": request_path,
            "manifest": manifest_path,
            "state-evidence": state_evidence_path,
            **({"initial-state": initial_state} if initial_state else {}),
            "output": output,
            "evidence-root": evidence_dir,
            "index": evidence_dir / "regression-recovery-evidence-index.json",
        }
    )
    initial_payload: dict[str, Any] | None = None
    if initial_state is not None:
        payload = _read_json(initial_state)
        if not isinstance(payload, dict):
            raise typer.BadParameter("initial state must be a JSON object")
        initial_payload = payload
    manifest = parse_config_manifest_yaml(manifest_path.read_text(encoding="utf-8"))
    state_evidence = _load_state_evidence(state_evidence_path)
    pinned_items, ownership_unknown_items = state_evidence.safety_constraints(
        manifest,
        target_id=target_id,
        actor_id=owner_id,
        environment_digest=_current_environment_digest(),
    )
    backend = (
        SimulatedBackend(initial_payload, target_id=target_id)
        if backend_kind == "simulated"
        else _local_backend(
            manifest,
            target_id=target_id,
            allowed_executables=allow_executable or [],
            writable_roots=list(writable_root),
        )
    )
    controller = SafetyController(
        SafetyPolicy(
            allow_keep=True,
            pinned_items=sorted(pinned_items),
            ownership_unknown_items=sorted(ownership_unknown_items),
        )
    )
    guard = FileTargetGuard(lease_root)
    lease = guard.acquire(
        target_id,
        owner_id,
        ttl_seconds=lease_ttl_seconds,
        now=datetime.now(UTC),
        reconciliation=None,
    )
    outcome: RegressionRecoveryOutcome | None = None
    pending_error: BaseException | None = None
    try:
        try:
            outcome = execute_regression_recovery(
                request,
                manifest=manifest,
                controller=controller,
                backend=backend,
                fencing_token=lease.fencing_token,
                recorded_at=datetime.now(UTC),
            )
        except Exception as error:
            reason = (
                f"L6c recovery execution failed for request {request.digest}: "
                f"{type(error).__name__}: {error}"
            )
            _mark_regression_attention(
                guard,
                target_id=target_id,
                reason=reason,
                evidence_digest=request.digest,
                primary_error=error,
            )
            raise RuntimeError(reason) from error

        try:
            graph = _build_regression_recovery_evidence_graph(request, outcome)
            _persist_regression_recovery_evidence_graph(evidence_dir, graph)
        except Exception as error:
            reason = (
                f"L6c evidence publication failed for request {request.digest}; "
                f"recovery={outcome.status.value}: {outcome.reason}; "
                f"evidence={type(error).__name__}: {error}"
            )
            if outcome.status is not RegressionRecoveryStatus.NOT_TRIGGERED:
                _mark_regression_attention(
                    guard,
                    target_id=target_id,
                    reason=reason,
                    evidence_digest=request.digest,
                    primary_error=error,
                )
            raise RuntimeError(reason) from error

        try:
            _write_json_atomic(output, outcome)
        except Exception as error:
            reason = (
                "L6c output convenience copy failed after complete evidence "
                f"publication: {type(error).__name__}: {error}"
            )
            if outcome.status is RegressionRecoveryStatus.NEEDS_ATTENTION:
                _mark_regression_attention(
                    guard,
                    target_id=target_id,
                    reason=f"{outcome.reason}; {reason}",
                    evidence_digest=outcome.digest,
                    primary_error=error,
                )
            raise RuntimeError(reason) from error

        if outcome.status is RegressionRecoveryStatus.NEEDS_ATTENTION:
            _mark_regression_attention(
                guard,
                target_id=target_id,
                reason=(
                    f"L6c recovery needs attention for request {request.digest}: "
                    f"{outcome.reason}"
                ),
                evidence_digest=outcome.digest,
            )
            raise typer.BadParameter(outcome.reason)
    except BaseException as error:
        pending_error = error
        raise
    finally:
        try:
            guard.release(lease)
        except Exception as release_error:
            if pending_error is not None:
                raise RuntimeError(
                    f"{type(pending_error).__name__}: {pending_error}; "
                    f"lease release failed: {release_error}"
                ) from pending_error
            raise
    console.print_json(
        json.dumps(
            {
                "status": outcome.status.value,
                "stop_required": outcome.stop_required,
            }
        )
    )


@system_opt_app.command("dynamic-reactivate")
def evaluate_dynamic_reactivation(
    run_path: Path = typer.Option(..., "--run", exists=True, dir_okay=False),
    gate_contract_path: Path = typer.Option(
        ..., "--gate-contract", exists=True, dir_okay=False
    ),
    max_reactivations: int = typer.Option(..., "--max-reactivations", min=0),
    slo_violation_windows: int = typer.Option(..., "--slo-violation-windows", min=1),
    reactivations_used: int = typer.Option(0, "--reactivations-used", min=0),
    windows_since_stop: int = typer.Option(..., "--windows-since-stop", min=0),
    consecutive_slo_violations: int | None = typer.Option(
        None,
        "--consecutive-slo-violations",
        min=0,
        help="observed violations since the stop; default derives the trailing violations",
    ),
    identity_drift_events_since_stop: int = typer.Option(
        0, "--identity-drift-events-since-stop", min=0
    ),
    output: Path = typer.Option(..., "--output", dir_okay=False),
) -> None:
    """Judge between-phase reactivation eligibility for a stopped dynamic phase.

    D5: eligibility never auto-restarts anything — whether to reopen the
    phase is the task owner's decision. Windows observed after the stop are
    operator-recorded facts (``--windows-since-stop`` etc.); the trailing
    SLO violations of the phase itself seed ``consecutive_slo_violations``
    unless the operator supplies an explicit count.
    """

    from looper_core.system_opt.dynamic_loop import DynamicPhaseRun
    from looper_core.system_opt.reactivation import (
        ReactivationPolicy,
        ReactivationState,
        evaluate_reactivation,
    )

    run = DynamicPhaseRun.model_validate(_read_json(run_path))
    gate_contract = DynamicPhaseGateContract.model_validate_json(
        gate_contract_path.read_text(encoding="utf-8")
    )
    if (
        run.stop_gate_decision is None
        or run.stop_gate_decision.contract_digest != gate_contract.digest
    ):
        raise typer.BadParameter(
            "the run was not gated by this gate contract (digest mismatch)"
        )
    violations = consecutive_slo_violations
    if violations is None:
        violations = 0
        for record in reversed(run.windows):
            if record.slo_met is False:
                violations += 1
            else:
                break
    decision = evaluate_reactivation(
        gate_contract,
        ReactivationPolicy(
            max_reactivations=max_reactivations,
            slo_violation_windows=slo_violation_windows,
        ),
        ReactivationState(
            reactivations_used=reactivations_used,
            windows_since_stop=windows_since_stop,
            consecutive_slo_violations=violations,
            identity_drift_events_since_stop=identity_drift_events_since_stop,
        ),
        evidence_digest=run.digest,
    )
    _write_json(output, decision)
    console.print_json(
        json.dumps(
            {
                "eligible": decision.eligible,
                "trigger": decision.trigger.value if decision.trigger else None,
                "reason": decision.reason,
                "note": "eligibility is not an auto-restart; the task owner decides",
                "decision_digest": decision.digest,
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


@system_opt_app.command("engine-demo")
def run_system_optimizer_engine_demo(
    output: Path = typer.Option(..., "--output", dir_okay=False),
    component: list[str] = typer.Option(["cpu", "memory"], "--component"),
    max_rounds: int = typer.Option(10, "--max-rounds", min=1),
    max_pool_size: int = typer.Option(64, "--max-pool-size", min=1),
) -> None:
    """Run the L8 engine loop over a synthetic multi-component demo.

    Simulated backend only: no Linux writes, safe on the Windows dev host.
    """

    from looper_core.system_opt.tuning import SystemOptimizationEngine

    manifest = build_demo_manifest()
    backend = SimulatedBackend(
        {item.id: item.default for item in manifest.items}, target_id="engine-demo"
    )
    optimizers = []
    for name in component:
        policy = build_demo_policy(OptimizationMode.GENERAL)
        policy.authorized_components = [name]
        policy.search.max_candidates = 2
        policy.search.max_attempts = 4
        policy.search.no_improvement_limit = 3
        policy.search.target_improvement = None
        optimizers.append(
            ComponentOptimizer(
                SystemOptimizationEngine(policy, manifest, resolve_demo_domains(manifest), backend)
            )
        )
    defaults = {item.parameter_id: item.default for item in manifest.items}
    result = run_engine_loop(
        optimizers,
        baseline_parameters={name: defaults for name in component},
        measures={
            name: SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL)
            for name in component
        },
        negative_cache=NegativeCache(),
        config=EngineLoopConfig(
            environment_digest=canonical_digest({"kind": "synthetic-engine-demo"}),
            formula_versions={"F-DEMO-LOOP": "v0"},
            pressure_protocol_digests={
                name: canonical_digest({"component": name, "kind": "demo-protocol"})
                for name in component
            },
            max_rounds=max_rounds,
            max_pool_size=max_pool_size,
        ),
        fencing_token=1,
    )
    _write_json(output, result.model_dump(mode="json"))
    console.print_json(
        json.dumps(
            {
                "evidence_kind": "synthetic",
                "warning": "not a Linux performance result",
                "stop_reason": result.stop_reason.value,
                "rounds": [
                    {"component": record.component, "verdicts": len(record.verdicts)}
                    for record in result.rounds
                ],
                "phase_restoration": (
                    result.phase_restoration.status.value
                    if result.phase_restoration is not None
                    else None
                ),
                "phase_note": result.phase_verification_note,
                "output": str(output.resolve()),
            }
        )
    )


@system_opt_app.command("cache-inspect")
def inspect_negative_cache_command(
    path: Path = typer.Option(..., "--path", exists=True, dir_okay=False),
) -> None:
    """Summarize an append-only negative result cache (L7)."""

    cache = NegativeCache.load(path)
    verdict_counts: dict[str, int] = {}
    environments: set[str] = set()
    metrics: set[str] = set()
    for entry in cache.entries:
        verdict_counts[entry.verdict.value] = (
            verdict_counts.get(entry.verdict.value, 0) + 1
        )
        environments.add(entry.identity.environment_digest)
        metrics.add(entry.metric_id)
    console.print_json(
        json.dumps(
            {
                "entries": len(cache),
                "verdict_counts": verdict_counts,
                "distinct_environments": len(environments),
                "distinct_metrics": sorted(metrics),
                "path": str(path.resolve()),
            }
        )
    )


if __name__ == "__main__":
    app()
