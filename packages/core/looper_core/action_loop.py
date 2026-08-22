from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from statistics import median
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from looper_core.analysis import paired_bootstrap_improvement
from looper_core.canonical import canonical_digest, utc_now_iso
from looper_core.contracts import Aggregation, Comparison, Direction


class ActionLoopError(RuntimeError):
    pass


class ActionDecision(StrEnum):
    ACCEPTED = "accepted"
    ROLLED_BACK = "rolled_back"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"


class ActionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: dict[str, Any]
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @classmethod
    def from_value(cls, value: Mapping[str, Any]) -> ActionState:
        copied = dict(value)
        return cls(value=copied, digest=canonical_digest(copied))


class ActionMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary: float
    secondary: float | None = None
    gates: dict[str, bool] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)


class VerificationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repeats: int = Field(default=3, ge=2, le=100)
    minimum_improvement_ratio: float = Field(default=0.05, ge=0)
    maximum_secondary_regression_ratio: float | None = Field(default=None, ge=0)
    confidence_level: float = Field(default=0.95, gt=0.5, lt=1)
    bootstrap_resamples: int = Field(default=1000, ge=100, le=100000)
    random_seed: int = Field(default=20260822, ge=0)


class ReversibleAction(Protocol):
    action_id: str

    def snapshot(self) -> ActionState: ...

    def apply(self, candidate: Mapping[str, Any]) -> ActionState: ...

    def readback(self) -> ActionState: ...

    def rollback(self, snapshot: ActionState) -> ActionState: ...


MeasurementRunner = Callable[[ActionState, int, str], ActionMeasurement]


@dataclass
class _AuditTrail:
    entries: list[dict[str, Any]] = field(default_factory=list)

    def append(self, event: str, **payload: Any) -> None:
        self.entries.append({"at": utc_now_iso(), "event": event, **payload})


def _measure_group(
    runner: MeasurementRunner,
    state: ActionState,
    repeats: int,
    phase: str,
    audit: _AuditTrail,
) -> list[ActionMeasurement]:
    measurements: list[ActionMeasurement] = []
    for repeat_index in range(repeats):
        measurement = runner(state, repeat_index, phase)
        measurements.append(measurement)
        audit.append(
            "measurement.completed",
            phase=phase,
            repeatIndex=repeat_index,
            stateDigest=state.digest,
            primary=measurement.primary,
            secondary=measurement.secondary,
            gates=measurement.gates,
            evidence=measurement.evidence,
        )
    return measurements


def _verification(
    baseline: list[ActionMeasurement],
    candidate: list[ActionMeasurement],
    policy: VerificationPolicy,
) -> dict[str, Any]:
    baseline_primary = [item.primary for item in baseline]
    candidate_primary = [item.primary for item in candidate]
    confidence = paired_bootstrap_improvement(
        candidate_primary,
        baseline_primary,
        Direction.MAXIMIZE,
        Aggregation.MEDIAN,
        Comparison.RELATIVE,
        policy.confidence_level,
        policy.bootstrap_resamples,
        policy.random_seed,
    )
    failed_gates = sorted(
        {
            gate
            for measurement in candidate
            for gate, passed in measurement.gates.items()
            if not passed
        }
    )
    secondary: dict[str, Any] | None = None
    secondary_passed = True
    baseline_secondary = [item.secondary for item in baseline if item.secondary is not None]
    candidate_secondary = [item.secondary for item in candidate if item.secondary is not None]
    if baseline_secondary or candidate_secondary:
        if len(baseline_secondary) != len(baseline) or len(candidate_secondary) != len(candidate):
            secondary_passed = False
            secondary = {"status": "incomplete"}
        else:
            baseline_value = median(baseline_secondary)
            candidate_value = median(candidate_secondary)
            regression = (
                (candidate_value - baseline_value) / abs(baseline_value)
                if baseline_value != 0
                else None
            )
            limit = policy.maximum_secondary_regression_ratio
            secondary_passed = regression is not None and limit is not None and regression <= limit
            secondary = {
                "status": "available",
                "direction": "minimize",
                "baseline": baseline_value,
                "candidate": candidate_value,
                "regressionRatio": regression,
                "maximumRegressionRatio": limit,
                "passed": secondary_passed,
            }
    return {
        "primary": {
            "direction": "maximize",
            "baseline": median(baseline_primary),
            "candidate": median(candidate_primary),
            "minimumImprovementRatio": policy.minimum_improvement_ratio,
            **confidence,
        },
        "hardGates": {"passed": not failed_gates, "failed": failed_gates},
        "secondary": secondary,
        "secondaryPassed": secondary_passed,
    }


def _rollback_and_verify(
    action: ReversibleAction,
    baseline_state: ActionState,
    audit: _AuditTrail,
) -> tuple[ActionState, bool]:
    restored = action.rollback(baseline_state)
    audit.append("action.rollback.applied", stateDigest=restored.digest)
    readback = action.readback()
    verified = readback.digest == baseline_state.digest
    audit.append(
        "action.rollback.verified",
        expectedDigest=baseline_state.digest,
        observedDigest=readback.digest,
        passed=verified,
    )
    return readback, verified


def execute_verified_action(
    action: ReversibleAction,
    candidate: Mapping[str, Any],
    runner: MeasurementRunner,
    policy: VerificationPolicy,
) -> dict[str, Any]:
    """Execute a finite, reversible action and keep it only after paired verification.

    Inconclusive evidence fails closed: the baseline state is restored. The returned
    document is deliberately self-contained so it can be persisted as audit evidence.
    """

    audit = _AuditTrail()
    baseline_state = action.snapshot()
    audit.append("action.snapshot.created", stateDigest=baseline_state.digest)
    observed_baseline = action.readback()
    if observed_baseline.digest != baseline_state.digest:
        raise ActionLoopError("baseline readback does not match the action snapshot")
    requested_candidate = ActionState.from_value(candidate)
    if requested_candidate.digest == baseline_state.digest:
        raise ActionLoopError("candidate action is identical to the baseline state")

    baseline_measurements: list[ActionMeasurement] = []
    candidate_measurements: list[ActionMeasurement] = []
    verification: dict[str, Any] | None = None
    final_state = baseline_state
    rollback_verified: bool | None = None
    decision = ActionDecision.FAILED
    reason = "action loop did not reach a decision"

    try:
        baseline_measurements = _measure_group(
            runner, baseline_state, policy.repeats, "baseline", audit
        )
        failed_baseline_gates = sorted(
            {
                gate
                for measurement in baseline_measurements
                for gate, passed in measurement.gates.items()
                if not passed
            }
        )
        if failed_baseline_gates:
            raise ActionLoopError(f"baseline failed hard gates: {', '.join(failed_baseline_gates)}")
        applied = action.apply(candidate)
        audit.append(
            "action.applied",
            requestedDigest=requested_candidate.digest,
            observedDigest=applied.digest,
        )
        readback = action.readback()
        if readback.digest != requested_candidate.digest:
            raise ActionLoopError("candidate readback does not match the requested action")
        audit.append("action.readback.verified", stateDigest=readback.digest, passed=True)

        candidate_measurements = _measure_group(
            runner, readback, policy.repeats, "candidate", audit
        )
        verification = _verification(baseline_measurements, candidate_measurements, policy)
        primary = verification["primary"]
        gates_passed = bool(verification["hardGates"]["passed"])
        secondary_passed = bool(verification["secondaryPassed"])
        if not gates_passed:
            decision = ActionDecision.ROLLED_BACK
            reason = "candidate failed one or more hard gates"
        elif not secondary_passed:
            decision = ActionDecision.ROLLED_BACK
            reason = "candidate exceeded the secondary-metric regression limit"
        elif float(primary["lower"]) >= policy.minimum_improvement_ratio:
            decision = ActionDecision.ACCEPTED
            reason = "candidate improvement passed the confidence and safety gates"
            final_state = readback
            audit.append("action.accepted", stateDigest=final_state.digest)
        elif float(primary["upper"]) < policy.minimum_improvement_ratio:
            decision = ActionDecision.ROLLED_BACK
            reason = "candidate did not reach the minimum effective improvement"
        else:
            decision = ActionDecision.INCONCLUSIVE
            reason = "candidate confidence interval crosses the decision threshold"

        if decision != ActionDecision.ACCEPTED:
            final_state, rollback_verified = _rollback_and_verify(action, baseline_state, audit)
            if not rollback_verified:
                decision = ActionDecision.FAILED
                reason = "rollback readback did not match the baseline snapshot"
    except Exception as error:
        reason = f"action or measurement failed: {error}"
        audit.append("action.failed", error=str(error))
        try:
            final_state, rollback_verified = _rollback_and_verify(action, baseline_state, audit)
            decision = ActionDecision.ROLLED_BACK if rollback_verified else ActionDecision.FAILED
        except Exception as rollback_error:
            decision = ActionDecision.FAILED
            rollback_verified = False
            reason = f"{reason}; rollback failed: {rollback_error}"
            audit.append("action.rollback.failed", error=str(rollback_error))

    return {
        "schemaVersion": "looper.verified-action/v1alpha1",
        "actionId": action.action_id,
        "decision": decision,
        "reason": reason,
        "baselineState": baseline_state.model_dump(mode="json"),
        "requestedCandidate": requested_candidate.model_dump(mode="json"),
        "finalState": final_state.model_dump(mode="json"),
        "rollbackVerified": rollback_verified,
        "verification": verification,
        "measurements": {
            "baseline": [item.model_dump(mode="json") for item in baseline_measurements],
            "candidate": [item.model_dump(mode="json") for item in candidate_measurements],
        },
        "auditTrail": audit.entries,
    }


class JsonFileAction:
    """A bounded actuator used by the local vertical slice.

    The file is a durable stand-in for an application configuration. Writes are
    atomic and every state is validated before it becomes active.
    """

    def __init__(
        self,
        action_id: str,
        path: Path,
        validator: Callable[[Mapping[str, Any]], dict[str, Any]],
        initial_state: Mapping[str, Any],
    ) -> None:
        self.action_id = action_id
        self.path = path.resolve()
        self._validator = validator
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(self._validator(initial_state))
        self.readback()

    def _write(self, value: Mapping[str, Any]) -> ActionState:
        import json
        import os
        import uuid

        validated = self._validator(value)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(validated, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        return self.readback()

    def snapshot(self) -> ActionState:
        return self.readback()

    def apply(self, candidate: Mapping[str, Any]) -> ActionState:
        return self._write(candidate)

    def readback(self) -> ActionState:
        import json

        document = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ActionLoopError("action state must be a JSON object")
        return ActionState.from_value(self._validator(document))

    def rollback(self, snapshot: ActionState) -> ActionState:
        return self._write(snapshot.value)
