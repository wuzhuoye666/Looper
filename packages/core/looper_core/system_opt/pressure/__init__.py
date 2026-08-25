"""L3 压力器 —— 层包（含接口与调用规范）。

调用规范：对上提供 StandardPressureProtocol（phases+stability 合同）与
S1.1 CV 门限派生；直接调用现成压力工具（stress-ng/sysbench/fio/iperf3/numactl），
经 executor 白名单 runner 执行；report-only 禁带 acceptance_limit、hard-gate
必带显式阈值、独占窗口前后干扰检查、cleanup 永远执行；禁止评价收益或判定
候选（L8 职责）。压/采解耦等 L4 新合同（SO-D016）。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from enum import StrEnum
from statistics import mean, median, stdev
from typing import Literal, Protocol

import yaml
from pydantic import Field, field_validator, model_validator

from looper_core.analysis import quantile
from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.collector import (
    COLLECTION_BUNDLE_MEDIA_TYPE,
    CollectionInputArtifact,
    CollectionMeasurementEnvelope,
    CollectionOverheadABEvidence,
    ComponentCollectionPlan,
    ComponentCollectionRequest,
    ComponentCollectionRun,
    ComponentCollectionScope,
    WindowedComponentCollector,
    begin_component_collection,
    bind_collection_to_measurement_batch,
    verify_collection_artifact_bundle,
)
from looper_core.system_opt.config_manifest import ConfigComponent
from looper_core.system_opt.executor import CommandRunner, OperationStatus
from looper_core.system_opt.measurement import MeasurementCommandSpec
from looper_core.system_opt.policy import SystemOptimizationPolicy
from looper_core.system_opt.scoring import (
    MeasurementBatch,
    MeasurementPhaseEvidence,
    MeasurementStabilityEvidence,
)

STANDARD_PRESSURE_PROTOCOL_SCHEMA = "looper.standard-pressure-protocol/v1alpha1"
PRESSURE_EXECUTION_EVIDENCE_SCHEMA = "looper.pressure-execution-evidence/v1alpha1"
PRESSURE_COLLECTION_RESULT_SCHEMA = "looper.pressure-collection-result/v1alpha1"
_STANDARD_COMPONENTS = {
    ConfigComponent.CPU,
    ConfigComponent.MEMORY,
    ConfigComponent.NUMA,
    ConfigComponent.STORAGE,
    ConfigComponent.NETWORK,
}


class PressureProtocolError(ValueError):
    pass


class PressurePhaseKind(StrEnum):
    PREPARE = "prepare"
    WARMUP = "warmup"
    MEASURE = "measure"
    VERIFY = "verify"
    COOLDOWN = "cooldown"
    CLEANUP = "cleanup"


class PressurePhaseSpec(StrictModel):
    id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9._-]*$")
    kind: PressurePhaseKind
    command: MeasurementCommandSpec
    declared_duration_seconds: float = Field(ge=0, le=86400)
    purpose: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_duration(self) -> PressurePhaseSpec:
        if (
            self.kind
            in {
                PressurePhaseKind.WARMUP,
                PressurePhaseKind.MEASURE,
                PressurePhaseKind.COOLDOWN,
            }
            and self.declared_duration_seconds <= 0
        ):
            raise ValueError(f"{self.kind.value} requires an explicit positive duration")
        if self.command.timeout_seconds <= self.declared_duration_seconds:
            raise ValueError("phase command timeout must exceed its declared duration")
        return self


class StabilityCalibrationContract(StrictModel):
    metric_id: str = Field(min_length=1, max_length=160)
    statistic: Literal["cv", "p95-over-median"]
    enforcement: Literal["report-only", "hard-gate"]
    acceptance_limit: float | None = Field(default=None, gt=0)
    minimum_repeats: int = Field(ge=2, le=10000)
    maximum_repeats: int = Field(ge=2, le=10000)
    source: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_repeat_range(self) -> StabilityCalibrationContract:
        if self.minimum_repeats > self.maximum_repeats:
            raise ValueError("stability minimum_repeats cannot exceed maximum_repeats")
        if self.enforcement == "hard-gate" and self.acceptance_limit is None:
            raise ValueError("hard-gate stability requires an explicit acceptance_limit")
        if self.enforcement == "report-only" and self.acceptance_limit is not None:
            raise ValueError("report-only stability cannot declare an acceptance_limit")
        return self


class StabilityLimitCalibrationEvidence(StrictModel):
    schema_version: Literal["looper.pressure-stability-calibration/v1alpha1"] = (
        "looper.pressure-stability-calibration/v1alpha1"
    )
    metric_id: str = Field(min_length=1, max_length=160)
    statistic: Literal["cv"]
    formula_id: Literal["F-PROJECT-PRESSURE-CV-BOOTSTRAP-UPPER/v1alpha1"]
    input_batch_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sample_count: int = Field(ge=3)
    observed_value: float = Field(ge=0)
    confidence_level: float = Field(gt=0.5, lt=1)
    bootstrap_resamples: int = Field(ge=100, le=100000)
    random_seed: int = Field(ge=0)
    acceptance_limit: float = Field(gt=0)
    target_scope: str = Field(min_length=1, max_length=1000)
    portability: str = Field(min_length=1, max_length=1000)

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class PressureArtifactRequirement(StrictModel):
    """One exact pressure-tool artifact that the selected L4 collector must parse."""

    artifact_id: str = Field(min_length=1, max_length=160)
    media_type: str = Field(min_length=1, max_length=120)


class PressureCollectionContract(StrictModel):
    """Additive L3 declaration of the L4 collection required by this protocol."""

    collector_id: str = Field(min_length=1, max_length=160)
    requested_metrics: list[str] = Field(min_length=1)
    artifact_requirements: list[PressureArtifactRequirement] = Field(min_length=1, max_length=1)
    interval_seconds: float = Field(gt=0)
    scope: ComponentCollectionScope
    workload_source: str = Field(min_length=1, max_length=300)

    @field_validator("requested_metrics")
    @classmethod
    def validate_requested_metrics(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("collection requested_metrics must not contain duplicates")
        return values

    @field_validator("interval_seconds")
    @classmethod
    def require_finite_interval(cls, value: float) -> float:
        if value == float("inf") or value != value:
            raise ValueError("collection interval_seconds must be finite")
        return value

    @model_validator(mode="after")
    def validate_artifact_requirements(self) -> PressureCollectionContract:
        artifact_ids = [item.artifact_id for item in self.artifact_requirements]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("artifact_requirements artifact_id values must be unique")
        if self.artifact_requirements[0].media_type != COLLECTION_BUNDLE_MEDIA_TYPE:
            raise ValueError("collection requires one ZIP artifact bundle media type")
        return self


class PressureExecutionEvidence(StrictModel):
    """Raw measure-phase handoff: identities, digests, and gates, but no metric parsing."""

    schema_version: Literal[PRESSURE_EXECUTION_EVIDENCE_SCHEMA] = PRESSURE_EXECUTION_EVIDENCE_SCHEMA
    protocol_id: str = Field(min_length=1, max_length=160)
    component: ConfigComponent
    workload_phase_id: str = Field(min_length=1, max_length=160)
    measurement_identity: dict[str, str] = Field(min_length=1)
    artifacts: list[CollectionInputArtifact] = Field(min_length=1, max_length=1)
    gate_values: dict[str, float | bool | None]

    @model_validator(mode="after")
    def validate_artifact_ids(self) -> PressureExecutionEvidence:
        artifact_ids = [artifact.artifact_id for artifact in self.artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("pressure execution artifact_id values must be unique")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class StandardPressureProtocol(StrictModel):
    schema_version: Literal[STANDARD_PRESSURE_PROTOCOL_SCHEMA]
    id: str = Field(min_length=1, max_length=160, pattern=r"^[a-z][a-z0-9.-]*$")
    component: ConfigComponent
    target_scope: str = Field(min_length=1, max_length=1000)
    limitation: str = Field(min_length=1, max_length=2000)
    required_executables: list[str] = Field(min_length=1)
    input_identity: dict[str, str] = Field(min_length=1)
    metric_ids: list[str] = Field(min_length=1)
    gate_metric_ids: list[str] = Field(min_length=1)
    stability: StabilityCalibrationContract
    phases: list[PressurePhaseSpec] = Field(min_length=4)
    collection: PressureCollectionContract | None = None

    @field_validator("required_executables", "metric_ids", "gate_metric_ids")
    @classmethod
    def reject_duplicates(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("pressure protocol lists must not contain duplicates")
        return values

    @model_validator(mode="after")
    def validate_protocol(self) -> StandardPressureProtocol:
        if self.component not in _STANDARD_COMPONENTS:
            raise ValueError("standard pressure supports cpu, memory, numa, storage, or network")
        phase_ids = [phase.id for phase in self.phases]
        if len(phase_ids) != len(set(phase_ids)):
            raise ValueError("pressure phase ids must be unique")
        kinds = [phase.kind for phase in self.phases]
        if kinds.count(PressurePhaseKind.WARMUP) != 1:
            raise ValueError("pressure protocol requires exactly one warmup phase")
        if kinds.count(PressurePhaseKind.MEASURE) != 1:
            raise ValueError("pressure protocol requires exactly one measure phase")
        if kinds.count(PressurePhaseKind.CLEANUP) != 1:
            raise ValueError("pressure protocol requires exactly one cleanup phase")
        if kinds[-1] != PressurePhaseKind.CLEANUP:
            raise ValueError("cleanup must be the final pressure phase")
        if kinds.index(PressurePhaseKind.WARMUP) > kinds.index(PressurePhaseKind.MEASURE):
            raise ValueError("warmup must precede measurement")
        if self.stability.metric_id not in self.metric_ids:
            raise ValueError("stability metric must be declared in metric_ids")
        if not set(self.gate_metric_ids) <= set(self.metric_ids):
            raise ValueError("gate_metric_ids must be included in metric_ids")
        if self.collection is not None:
            requested = set(self.collection.requested_metrics)
            prefix = f"{self.component.value}."
            if any(not metric.startswith(prefix) for metric in requested):
                raise ValueError(
                    "collection requested_metrics must belong to the protocol component"
                )
            if not set(self.metric_ids) <= requested:
                raise ValueError("metric_ids must be included in collection requested_metrics")
            # Reuse the authoritative L4 scope contract instead of guessing interface/device scope.
            ComponentCollectionPlan(
                component=self.component.value,
                target_id="protocol-validation",
                environment_digest="sha256:" + "0" * 64,
                workload_phase_id=self.measurement_phase.id,
                workload_source=self.collection.workload_source,
                collector_id=self.collection.collector_id,
                requested_metrics=self.collection.requested_metrics,
                interval_seconds=self.collection.interval_seconds,
                scope=self.collection.scope,
            )
        phase_executables = {phase.command.argv[0] for phase in self.phases}
        if not phase_executables <= set(self.required_executables):
            raise ValueError("every phase executable must be explicitly required")
        return self

    @property
    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        # ``collection`` is additive.  Legacy v1alpha1 documents retain their frozen digest.
        if self.collection is None:
            payload.pop("collection", None)
        return canonical_digest(payload)

    @property
    def measurement_phase(self) -> PressurePhaseSpec:
        return next(phase for phase in self.phases if phase.kind == PressurePhaseKind.MEASURE)


def validate_pressure_policy(
    protocol: StandardPressureProtocol, policy: SystemOptimizationPolicy
) -> None:
    if protocol.stability.enforcement != "hard-gate":
        raise PressureProtocolError(
            "closed-loop pressure tuning requires an approved hard-gate stability contract"
        )
    if policy.id != protocol.input_identity.get("policy_id"):
        raise PressureProtocolError("pressure protocol is not bound to this policy id")
    if policy.authorized_components != [protocol.component.value]:
        raise PressureProtocolError(
            "one component pressure run requires exactly one matching authorized component"
        )
    policy_metric_ids = {metric.id for metric in policy.metrics}
    if set(protocol.metric_ids) != policy_metric_ids:
        raise PressureProtocolError(
            "pressure protocol metric ids must exactly match policy metrics"
        )
    gate_metric_ids = {gate.metric for gate in policy.hard_gates}
    if set(protocol.gate_metric_ids) != gate_metric_ids:
        raise PressureProtocolError(
            "pressure protocol gate metrics must exactly match policy gates"
        )
    if policy.primary_metric.component != protocol.component.value:
        raise PressureProtocolError("primary metric component does not match pressure component")
    repeats = {
        policy.statistics.baseline_repeats,
        policy.statistics.candidate_repeats,
    }
    if min(repeats) < protocol.stability.minimum_repeats:
        raise PressureProtocolError("policy repeats are below the stability calibration minimum")
    if max(repeats) > protocol.stability.maximum_repeats:
        raise PressureProtocolError("policy repeats exceed the calibrated stability range")


def evaluate_measurement_stability(
    batch: MeasurementBatch,
    contract: StabilityCalibrationContract,
) -> MeasurementStabilityEvidence:
    try:
        values = batch.metrics[contract.metric_id].values
    except KeyError as error:
        raise PressureProtocolError(f"stability metric is missing: {contract.metric_id}") from error
    if not contract.minimum_repeats <= len(values) <= contract.maximum_repeats:
        raise PressureProtocolError("measurement count is outside the calibrated repeat range")
    if contract.statistic == "cv":
        center = mean(values)
        if center == 0:
            raise PressureProtocolError("CV stability is undefined for a zero mean")
        value = stdev(values) / abs(center)
        formula_id = "F-PROJECT-PRESSURE-CV/v1alpha1"
    else:
        center = median(values)
        if center <= 0:
            raise PressureProtocolError("p95-over-median requires a positive median")
        value = quantile(values, 0.95) / center
        formula_id = "F-PROJECT-PRESSURE-P95-MEDIAN/v1alpha1"
    return MeasurementStabilityEvidence(
        metric_id=contract.metric_id,
        statistic=contract.statistic,
        formula_id=formula_id,
        sample_count=len(values),
        value=value,
        enforcement=contract.enforcement,
        acceptance_limit=contract.acceptance_limit,
        accepted=(
            value <= contract.acceptance_limit if contract.acceptance_limit is not None else None
        ),
    )


def calibrate_cv_acceptance_limit(
    batch: MeasurementBatch,
    metric_id: str,
    *,
    confidence_level: float,
    bootstrap_resamples: int,
    random_seed: int,
    target_scope: str,
    portability: str,
) -> StabilityLimitCalibrationEvidence:
    """Derive a target-local CV limit from one frozen calibration batch.

    Each bootstrap CV uses the mean and sample standard deviation from the
    same resample. The returned limit is the requested one-sided empirical
    upper quantile; no hidden rounding or safety multiplier is applied.
    """

    if not 0.5 < confidence_level < 1:
        raise PressureProtocolError("calibration confidence_level must be in (0.5, 1)")
    if not 100 <= bootstrap_resamples <= 100000:
        raise PressureProtocolError("calibration bootstrap_resamples must be in [100, 100000]")
    if random_seed < 0:
        raise PressureProtocolError("calibration random_seed must be non-negative")
    try:
        values = batch.metrics[metric_id].values
    except KeyError as error:
        raise PressureProtocolError(f"calibration metric is missing: {metric_id}") from error
    if len(values) < 3:
        raise PressureProtocolError("CV calibration requires at least three samples")
    center = mean(values)
    if center == 0:
        raise PressureProtocolError("CV calibration is undefined for a zero mean")
    observed = stdev(values) / abs(center)
    generator = random.Random(random_seed)
    estimates: list[float] = []
    for _ in range(bootstrap_resamples):
        sample = [generator.choice(values) for _ in values]
        sample_center = mean(sample)
        if sample_center == 0:
            raise PressureProtocolError(
                "CV bootstrap produced a zero mean; choose a non-zero stability metric"
            )
        estimates.append(stdev(sample) / abs(sample_center))
    acceptance_limit = quantile(estimates, confidence_level)
    if acceptance_limit <= 0:
        raise PressureProtocolError(
            "CV bootstrap upper limit is zero; the calibration cannot form a positive gate"
        )
    return StabilityLimitCalibrationEvidence(
        metric_id=metric_id,
        statistic="cv",
        formula_id="F-PROJECT-PRESSURE-CV-BOOTSTRAP-UPPER/v1alpha1",
        input_batch_digest=batch.digest,
        sample_count=len(values),
        observed_value=observed,
        confidence_level=confidence_level,
        bootstrap_resamples=bootstrap_resamples,
        random_seed=random_seed,
        acceptance_limit=acceptance_limit,
        target_scope=target_scope,
        portability=portability,
    )


class PressureCollectionResult(StrictModel):
    """One decoupled run; disabled runs retain workload evidence without inventing a batch."""

    schema_version: Literal[PRESSURE_COLLECTION_RESULT_SCHEMA] = PRESSURE_COLLECTION_RESULT_SCHEMA
    protocol_id: str = Field(min_length=1, max_length=160)
    protocol_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    phase_evidence: list[MeasurementPhaseEvidence] = Field(min_length=1)
    execution_evidence: PressureExecutionEvidence
    collection_run: ComponentCollectionRun
    envelope: CollectionMeasurementEnvelope | None
    elapsed_seconds: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_result_binding(self) -> PressureCollectionResult:
        execution = self.execution_evidence
        request = self.collection_run.request
        if self.protocol_id != execution.protocol_id:
            raise ValueError("result protocol_id is not bound to execution evidence")
        if request.component != execution.component.value:
            raise ValueError("collection request component is not bound to execution evidence")
        if request.workload_phase_id != execution.workload_phase_id:
            raise ValueError("collection request phase is not bound to execution evidence")
        if request.measurement_identity != execution.measurement_identity:
            raise ValueError("collection request identity is not bound to execution evidence")
        if request.input_artifacts != execution.artifacts:
            raise ValueError("collection request artifacts are not bound to execution evidence")
        if self.collection_run.enabled != (self.envelope is not None):
            raise ValueError("enabled collection and envelope presence must match")
        if self.envelope is not None:
            if self.envelope.collection_run != self.collection_run:
                raise ValueError("result envelope is not bound to collection_run")
            batch = self.envelope.measurement_batch
            if batch.pressure_protocol_digest != self.protocol_digest:
                raise ValueError("measurement batch is not bound to result protocol_digest")
            if batch.phase_evidence != self.phase_evidence:
                raise ValueError("measurement batch does not preserve result phase evidence")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


def _default_artifact_reader(artifact: CollectionInputArtifact) -> bytes:
    from pathlib import Path

    try:
        return Path(artifact.source).read_bytes()
    except OSError as error:
        raise PressureProtocolError(
            f"pressure artifact is unreadable: {artifact.artifact_id} ({artifact.source})"
        ) from error


class PhasedPressureCollectionAdapter:
    """Run L3 phases while an injected L4 session observes the real measure window."""

    def __init__(
        self,
        protocol: StandardPressureProtocol,
        runner: CommandRunner,
        *,
        collector: WindowedComponentCollector,
        target_id: str,
        environment_digest: str,
        collection_enabled: bool,
        artifact_reader: Callable[[CollectionInputArtifact], bytes] | None = None,
        monotonic: Callable[[], float] = time.perf_counter,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        if protocol.collection is None:
            raise PressureProtocolError(
                "decoupled pressure collection requires an explicit collection contract"
            )
        if collector.collector_id != protocol.collection.collector_id:
            raise ValueError("injected collector_id does not match pressure collection contract")
        self.protocol = protocol
        self.runner = runner
        self.collector = collector
        self.target_id = target_id
        self.environment_digest = environment_digest
        self.collection_enabled = collection_enabled
        self.artifact_reader = artifact_reader or _default_artifact_reader
        self.monotonic = monotonic
        self.wall_clock = wall_clock

    def _plan(self) -> ComponentCollectionPlan:
        contract = self.protocol.collection
        assert contract is not None
        return ComponentCollectionPlan(
            component=self.protocol.component.value,
            target_id=self.target_id,
            environment_digest=self.environment_digest,
            workload_phase_id=self.protocol.measurement_phase.id,
            workload_source=contract.workload_source,
            collector_id=contract.collector_id,
            requested_metrics=contract.requested_metrics,
            interval_seconds=contract.interval_seconds,
            scope=contract.scope,
        )

    def _parse_and_validate_execution(self, stdout: str) -> PressureExecutionEvidence:
        try:
            evidence = PressureExecutionEvidence.model_validate_json(stdout)
        except ValueError as error:
            raise PressureProtocolError(
                "pressure measure stdout is not PressureExecutionEvidence"
            ) from error
        if evidence.protocol_id != self.protocol.id:
            raise PressureProtocolError("pressure execution protocol_id does not match protocol")
        if evidence.component != self.protocol.component:
            raise PressureProtocolError("pressure execution component does not match protocol")
        if evidence.workload_phase_id != self.protocol.measurement_phase.id:
            raise PressureProtocolError("pressure execution workload phase does not match protocol")
        if set(evidence.gate_values) != set(self.protocol.gate_metric_ids):
            raise PressureProtocolError(
                "pressure execution gate_values must exactly match gate_metric_ids"
            )
        contract = self.protocol.collection
        assert contract is not None
        expected = {
            requirement.artifact_id: requirement.media_type
            for requirement in contract.artifact_requirements
        }
        actual = {artifact.artifact_id: artifact.media_type for artifact in evidence.artifacts}
        if actual != expected:
            raise PressureProtocolError(
                "pressure execution artifacts do not exactly match artifact_requirements"
            )
        for artifact in evidence.artifacts:
            try:
                content = self.artifact_reader(artifact)
            except PressureProtocolError:
                raise
            except Exception as error:
                raise PressureProtocolError(
                    f"pressure artifact reader failed: {artifact.artifact_id}"
                ) from error
            try:
                verify_collection_artifact_bundle(content, expected_digest=artifact.digest)
            except ValueError as error:
                raise PressureProtocolError(
                    f"pressure artifact digest or bundle validation failed: {artifact.artifact_id}"
                ) from error
        return evidence

    def _request(self, evidence: PressureExecutionEvidence) -> ComponentCollectionRequest:
        contract = self.protocol.collection
        assert contract is not None
        return ComponentCollectionRequest(
            component=self.protocol.component.value,
            target_id=self.target_id,
            environment_digest=self.environment_digest,
            workload_phase_id=self.protocol.measurement_phase.id,
            workload_source=contract.workload_source,
            collector_id=contract.collector_id,
            requested_metrics=contract.requested_metrics,
            input_artifacts=evidence.artifacts,
            gate_values=evidence.gate_values,
            interval_seconds=contract.interval_seconds,
            scope=contract.scope,
            measurement_identity=evidence.measurement_identity,
        )

    @staticmethod
    def _phase_record(phase: PressurePhaseSpec, repeats: int, result) -> MeasurementPhaseEvidence:
        rendered = phase.command.render(repeats)
        return MeasurementPhaseEvidence(
            phase_id=phase.id,
            kind=phase.kind.value,
            command_digest=canonical_digest(rendered),
            status=result.status.value,
            elapsed_seconds=result.elapsed_seconds,
        )

    def __call__(self, repeats: int) -> PressureCollectionResult:
        started = self.monotonic()
        records: list[MeasurementPhaseEvidence] = []
        execution: PressureExecutionEvidence | None = None
        collection_run: ComponentCollectionRun | None = None
        window = None
        error: Exception | None = None
        cleanup = self.protocol.phases[-1]
        try:
            for phase in self.protocol.phases[:-1]:
                if phase.kind == PressurePhaseKind.MEASURE:
                    window = begin_component_collection(
                        self._plan(),
                        collector=self.collector,
                        enabled=self.collection_enabled,
                        wall_clock=self.wall_clock,
                    )
                result = self.runner.run(
                    phase.command.render(repeats),
                    timeout_seconds=phase.command.timeout_seconds,
                )
                records.append(self._phase_record(phase, repeats, result))
                if result.status != OperationStatus.SUCCEEDED:
                    raise RuntimeError(
                        result.stderr or f"pressure phase {phase.id} {result.status.value}"
                    )
                if phase.kind == PressurePhaseKind.MEASURE:
                    assert window is not None
                    execution = self._parse_and_validate_execution(result.stdout)
                    collection_run = window.finish(self._request(execution))
                    window = None
        except Exception as caught:  # every failure still closes L4 and runs L3 cleanup
            error = caught
            if window is not None:
                try:
                    window.cancel()
                except Exception as cancel_error:
                    error.add_note(
                        "L4 collection cancellation also failed: "
                        f"{type(cancel_error).__name__}: {cancel_error}"
                    )

        cleanup_result = self.runner.run(
            cleanup.command.render(repeats),
            timeout_seconds=cleanup.command.timeout_seconds,
        )
        records.append(self._phase_record(cleanup, repeats, cleanup_result))
        elapsed_seconds = self.monotonic() - started
        if (
            elapsed_seconds < 0
            or elapsed_seconds == float("inf")
            or elapsed_seconds != elapsed_seconds
        ):
            raise RuntimeError("pressure collection elapsed time must be finite and non-negative")
        if cleanup_result.status != OperationStatus.SUCCEEDED:
            raise RuntimeError(
                cleanup_result.stderr
                or f"pressure cleanup {cleanup.id} {cleanup_result.status.value}"
            )
        if error is not None:
            raise error
        if execution is None or collection_run is None:
            raise RuntimeError("pressure protocol produced no execution/collection evidence")

        envelope: CollectionMeasurementEnvelope | None = None
        if collection_run.enabled:
            base = bind_collection_to_measurement_batch(
                collection_run,
                gate_values=execution.gate_values,
            )
            missing_main = sorted(
                set(self.protocol.metric_ids) - set(base.measurement_batch.metrics)
            )
            if missing_main:
                raise PressureProtocolError(
                    f"required pressure metrics are unavailable: {missing_main}"
                )
            payload = base.measurement_batch.model_dump(mode="python")
            payload.update(
                {
                    "pressure_protocol_digest": self.protocol.digest,
                    "phase_evidence": records,
                    "stability_evidence": None,
                }
            )
            batch = MeasurementBatch.model_validate(payload)
            stability = evaluate_measurement_stability(batch, self.protocol.stability)
            if self.protocol.stability.enforcement == "hard-gate" and not stability.accepted:
                raise PressureProtocolError(
                    f"measurement stability {stability.value} exceeds "
                    f"the explicit limit {stability.acceptance_limit}"
                )
            payload = batch.model_dump(mode="python")
            payload["stability_evidence"] = stability
            batch = MeasurementBatch.model_validate(payload)
            envelope = CollectionMeasurementEnvelope(
                collection_run=collection_run,
                measurement_batch=batch,
                measurement_batch_digest=batch.digest,
                collection_metric_names=base.collection_metric_names,
                unavailable_metrics=base.unavailable_metrics,
            )
        return PressureCollectionResult(
            protocol_id=self.protocol.id,
            protocol_digest=self.protocol.digest,
            phase_evidence=records,
            execution_evidence=execution,
            collection_run=collection_run,
            envelope=envelope,
            elapsed_seconds=elapsed_seconds,
        )


class CollectionOverheadRun(Protocol):
    """Structural input required by the raw collection-overhead evidence builder."""

    protocol_id: str
    protocol_digest: str
    collection_run: ComponentCollectionRun
    elapsed_seconds: float


def build_collection_overhead_evidence(
    disabled_runs: Sequence[CollectionOverheadRun],
    enabled_runs: Sequence[CollectionOverheadRun],
    *,
    collected_at: datetime,
) -> CollectionOverheadABEvidence:
    """Bind paired raw wall-times only; no threshold, delta, ratio, or verdict is derived."""

    if not disabled_runs or len(disabled_runs) != len(enabled_runs):
        raise ValueError("collection overhead runs must be non-empty and paired")
    if any(run.collection_run.enabled for run in disabled_runs):
        raise ValueError("disabled overhead arm contains an enabled collection run")
    if any(not run.collection_run.enabled for run in enabled_runs):
        raise ValueError("enabled overhead arm contains a disabled collection run")
    first = disabled_runs[0]
    reference_request = first.collection_run.request
    identity = {
        "protocol_id": first.protocol_id,
        "protocol_digest": first.protocol_digest,
        "measurement_identity_digest": canonical_digest(reference_request.measurement_identity),
    }
    for run in [*disabled_runs, *enabled_runs]:
        request = run.collection_run.request
        comparable = (
            run.protocol_id == first.protocol_id
            and run.protocol_digest == first.protocol_digest
            and request.plan == reference_request.plan
            and request.measurement_identity == reference_request.measurement_identity
        )
        if not comparable:
            raise ValueError("collection overhead arms do not describe the same workload identity")
    return CollectionOverheadABEvidence(
        target_id=reference_request.target_id,
        environment_digest=reference_request.environment_digest,
        workload_identity=identity,
        collector_id=reference_request.collector_id,
        collection_disabled_seconds=[run.elapsed_seconds for run in disabled_runs],
        collection_enabled_seconds=[run.elapsed_seconds for run in enabled_runs],
        collected_at=collected_at,
    )


class PhasedPressureMeasurementAdapter:
    """Execute an explicit phase contract and fail closed on cleanup failure.

    Exactly one phase emits the MeasurementBatch JSON. Every other phase is
    lifecycle control whose stdout is retained only through command digests and
    status evidence. Cleanup is attempted in ``finally`` even after a failed
    preparation, warmup, measurement, verification, or cooldown command.
    """

    def __init__(self, protocol: StandardPressureProtocol, runner: CommandRunner) -> None:
        self.protocol = protocol
        self.runner = runner

    def __call__(self, repeats: int) -> MeasurementBatch:
        records: list[MeasurementPhaseEvidence] = []
        batch: MeasurementBatch | None = None
        error: Exception | None = None
        cleanup = self.protocol.phases[-1]
        try:
            for phase in self.protocol.phases[:-1]:
                result = self.runner.run(
                    phase.command.render(repeats),
                    timeout_seconds=phase.command.timeout_seconds,
                )
                records.append(
                    MeasurementPhaseEvidence(
                        phase_id=phase.id,
                        kind=phase.kind.value,
                        command_digest=canonical_digest(phase.command.render(repeats)),
                        status=result.status.value,
                        elapsed_seconds=result.elapsed_seconds,
                    )
                )
                if result.status != OperationStatus.SUCCEEDED:
                    raise RuntimeError(
                        result.stderr or f"pressure phase {phase.id} {result.status.value}"
                    )
                if phase.kind == PressurePhaseKind.MEASURE:
                    try:
                        batch = MeasurementBatch.model_validate_json(result.stdout)
                    except ValueError as parse_error:
                        raise ValueError(
                            "pressure measurement stdout is not a MeasurementBatch"
                        ) from parse_error
        except Exception as caught:  # cleanup must run for every failure class
            error = caught
        cleanup_result = self.runner.run(
            cleanup.command.render(repeats),
            timeout_seconds=cleanup.command.timeout_seconds,
        )
        records.append(
            MeasurementPhaseEvidence(
                phase_id=cleanup.id,
                kind=cleanup.kind.value,
                command_digest=canonical_digest(cleanup.command.render(repeats)),
                status=cleanup_result.status.value,
                elapsed_seconds=cleanup_result.elapsed_seconds,
            )
        )
        if cleanup_result.status != OperationStatus.SUCCEEDED:
            raise RuntimeError(
                cleanup_result.stderr
                or f"pressure cleanup {cleanup.id} {cleanup_result.status.value}"
            )
        if error is not None:
            raise error
        if batch is None:
            raise RuntimeError("pressure protocol produced no measurement batch")
        if (
            batch.pressure_protocol_digest is not None
            and batch.pressure_protocol_digest != self.protocol.digest
        ):
            raise PressureProtocolError("measurement batch is not bound to the pressure protocol")
        stability = evaluate_measurement_stability(batch, self.protocol.stability)
        if self.protocol.stability.enforcement == "hard-gate" and not stability.accepted:
            raise PressureProtocolError(
                f"measurement stability {stability.value} exceeds "
                f"the explicit limit {stability.acceptance_limit}"
            )
        return batch.model_copy(
            update={
                "pressure_protocol_digest": self.protocol.digest,
                "phase_evidence": records,
                "stability_evidence": stability,
            }
        )


def parse_standard_pressure_protocol_yaml(content: str) -> StandardPressureProtocol:
    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise PressureProtocolError("standard pressure protocol YAML is invalid") from error
    if not isinstance(payload, dict):
        raise PressureProtocolError("standard pressure protocol YAML must contain one object")
    try:
        return StandardPressureProtocol.model_validate(payload)
    except ValueError as error:
        raise PressureProtocolError(str(error)) from error


__all__ = [
    "CollectionOverheadRun",
    "PhasedPressureCollectionAdapter",
    "PressureArtifactRequirement",
    "PressureCollectionContract",
    "PressureCollectionResult",
    "PressureExecutionEvidence",
    "PhasedPressureMeasurementAdapter",
    "PressurePhaseKind",
    "PressurePhaseSpec",
    "PressureProtocolError",
    "STANDARD_PRESSURE_PROTOCOL_SCHEMA",
    "StabilityLimitCalibrationEvidence",
    "StabilityCalibrationContract",
    "StandardPressureProtocol",
    "build_collection_overhead_evidence",
    "calibrate_cv_acceptance_limit",
    "parse_standard_pressure_protocol_yaml",
    "evaluate_measurement_stability",
    "validate_pressure_policy",
]
