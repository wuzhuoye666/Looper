from __future__ import annotations

from enum import StrEnum
from statistics import mean, median, stdev
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from looper_core.analysis import quantile
from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
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
        if self.kind in {
            PressurePhaseKind.WARMUP,
            PressurePhaseKind.MEASURE,
            PressurePhaseKind.COOLDOWN,
        } and self.declared_duration_seconds <= 0:
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
        phase_executables = {phase.command.argv[0] for phase in self.phases}
        if not phase_executables <= set(self.required_executables):
            raise ValueError("every phase executable must be explicitly required")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))

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
        raise PressureProtocolError(
            f"stability metric is missing: {contract.metric_id}"
        ) from error
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
            value <= contract.acceptance_limit
            if contract.acceptance_limit is not None
            else None
        ),
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
                        raise ValueError("pressure measurement stdout is not a MeasurementBatch") \
                            from parse_error
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
    "PhasedPressureMeasurementAdapter",
    "PressurePhaseKind",
    "PressurePhaseSpec",
    "PressureProtocolError",
    "STANDARD_PRESSURE_PROTOCOL_SCHEMA",
    "StabilityCalibrationContract",
    "StandardPressureProtocol",
    "parse_standard_pressure_protocol_yaml",
    "evaluate_measurement_stability",
    "validate_pressure_policy",
]
