from __future__ import annotations

import json
from pathlib import Path

import pytest
from looper_core.system_opt.demo import build_demo_policy
from looper_core.system_opt.executor import CommandResult, OperationStatus
from looper_core.system_opt.policy import OptimizationMode
from looper_core.system_opt.pressure import (
    PhasedPressureMeasurementAdapter,
    PressureProtocolError,
    StandardPressureProtocol,
    evaluate_measurement_stability,
    parse_standard_pressure_protocol_yaml,
    validate_pressure_policy,
)
from looper_core.system_opt.scoring import MeasurementBatch
from pydantic import ValidationError


def _payload() -> dict[str, object]:
    return {
        "schema_version": "looper.standard-pressure-protocol/v1alpha1",
        "id": "cpu-controlled-v1",
        "component": "cpu",
        "target_scope": "one isolated Linux guest",
        "limitation": "does not represent a cross-host or bare-metal result",
        "required_executables": ["prepare", "warmup", "measure", "verify", "cleanup"],
        "input_identity": {"policy_id": "synthetic-general-closed-loop"},
        "metric_ids": ["workload.score", "workload.latency-p99", "gate.correctness"],
        "gate_metric_ids": ["gate.correctness"],
        "stability": {
            "metric_id": "workload.score",
            "statistic": "cv",
            "enforcement": "hard-gate",
            "acceptance_limit": 0.05,
            "minimum_repeats": 5,
            "maximum_repeats": 9,
            "source": "explicit test calibration",
        },
        "phases": [
            {
                "id": "prepare",
                "kind": "prepare",
                "command": {"argv": ["prepare", "{repeats}"], "timeout_seconds": 2},
                "declared_duration_seconds": 0,
                "purpose": "freeze input",
            },
            {
                "id": "warmup",
                "kind": "warmup",
                "command": {"argv": ["warmup", "{repeats}"], "timeout_seconds": 3},
                "declared_duration_seconds": 1,
                "purpose": "discard cold state",
            },
            {
                "id": "measure",
                "kind": "measure",
                "command": {"argv": ["measure", "{repeats}"], "timeout_seconds": 4},
                "declared_duration_seconds": 2,
                "purpose": "emit one measurement batch",
            },
            {
                "id": "verify",
                "kind": "verify",
                "command": {"argv": ["verify", "{repeats}"], "timeout_seconds": 2},
                "declared_duration_seconds": 0,
                "purpose": "verify output integrity",
            },
            {
                "id": "cleanup",
                "kind": "cleanup",
                "command": {"argv": ["cleanup", "{repeats}"], "timeout_seconds": 2},
                "declared_duration_seconds": 0,
                "purpose": "remove all pressure processes and files",
            },
        ],
    }


class QueueRunner:
    def __init__(self, results: list[CommandResult]) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, timeout_seconds: float) -> CommandResult:
        self.calls.append(argv)
        return self.results.pop(0)


def _success(stdout: str = "") -> CommandResult:
    return CommandResult(
        status=OperationStatus.SUCCEEDED,
        exit_code=0,
        stdout=stdout,
        elapsed_seconds=0.1,
    )


def test_protocol_rejects_missing_cleanup() -> None:
    payload = _payload()
    payload["phases"] = payload["phases"][:-1]  # type: ignore[index]

    with pytest.raises(ValidationError, match="cleanup"):
        StandardPressureProtocol.model_validate(payload)


def test_protocol_requires_timeout_above_declared_duration() -> None:
    payload = _payload()
    payload["phases"][1]["command"]["timeout_seconds"] = 1  # type: ignore[index]

    with pytest.raises(ValidationError, match="timeout must exceed"):
        StandardPressureProtocol.model_validate(payload)


def test_protocol_and_policy_must_have_exact_component_and_metric_binding() -> None:
    protocol = StandardPressureProtocol.model_validate(_payload())
    policy = build_demo_policy(OptimizationMode.GENERAL)
    policy.authorized_components = ["cpu"]
    for metric in policy.metrics:
        metric.component = "cpu"

    validate_pressure_policy(protocol, policy)
    policy.authorized_components = ["cpu", "memory"]
    with pytest.raises(PressureProtocolError, match="exactly one"):
        validate_pressure_policy(protocol, policy)


def test_phased_adapter_records_every_phase_and_binds_protocol() -> None:
    protocol = StandardPressureProtocol.model_validate(_payload())
    measurement = {
        "identity": {"target": "t", "phase": "steady"},
        "metrics": {
            "workload.score": {
                "metric_id": "workload.score",
                "values": [1.0, 1.01, 1.0, 1.01, 1.0],
            },
            "gate.correctness": {"metric_id": "gate.correctness", "values": [1.0, 1.0]},
        },
        "gate_values": {"gate.correctness": True},
        "pressure_protocol_digest": protocol.digest,
    }
    runner = QueueRunner(
        [_success(), _success(), _success(json.dumps(measurement)), _success(), _success()]
    )

    batch = PhasedPressureMeasurementAdapter(protocol, runner)(2)

    assert [record.kind for record in batch.phase_evidence] == [
        "prepare",
        "warmup",
        "measure",
        "verify",
        "cleanup",
    ]
    assert [call[0] for call in runner.calls] == [
        "prepare",
        "warmup",
        "measure",
        "verify",
        "cleanup",
    ]
    assert batch.stability_evidence is not None
    assert batch.stability_evidence.accepted is True


def test_cv_stability_fails_when_explicit_limit_is_exceeded() -> None:
    protocol = StandardPressureProtocol.model_validate(_payload())
    measurement = {
        "identity": {"target": "t", "phase": "steady"},
        "metrics": {
            "workload.score": {
                "metric_id": "workload.score",
                "values": [1.0, 2.0, 1.0, 2.0, 1.0],
            }
        },
        "gate_values": {"gate.correctness": True},
    }
    batch = MeasurementBatch.model_validate(measurement)

    evidence = evaluate_measurement_stability(batch, protocol.stability)

    assert evidence.accepted is False
    assert evidence.value > evidence.acceptance_limit


def test_report_only_calibration_forbids_an_unapproved_threshold() -> None:
    payload = _payload()
    payload["stability"]["enforcement"] = "report-only"  # type: ignore[index]
    payload["stability"]["acceptance_limit"] = None  # type: ignore[index]

    protocol = StandardPressureProtocol.model_validate(payload)

    assert protocol.stability.acceptance_limit is None


def test_hard_gate_calibration_requires_a_threshold() -> None:
    payload = _payload()
    payload["stability"]["acceptance_limit"] = None  # type: ignore[index]

    with pytest.raises(ValidationError, match="requires an explicit"):
        StandardPressureProtocol.model_validate(payload)


@pytest.mark.parametrize(
    "filename,component",
    [
        ("cpu-pressure-calibration-protocol.yaml", "cpu"),
        ("memory-pressure-calibration-protocol.yaml", "memory"),
        ("network-loopback-calibration-protocol.yaml", "network"),
    ],
)
def test_repository_calibration_protocols_are_report_only(
    filename: str, component: str
) -> None:
    path = Path(__file__).parents[1] / "examples" / "system-optimizer" / filename

    protocol = parse_standard_pressure_protocol_yaml(path.read_text(encoding="utf-8"))

    assert protocol.component.value == component
    assert protocol.stability.enforcement == "report-only"
    assert protocol.stability.acceptance_limit is None


def test_phased_adapter_always_cleans_up_after_measurement_failure() -> None:
    protocol = StandardPressureProtocol.model_validate(_payload())
    failed = CommandResult(
        status=OperationStatus.FAILED,
        exit_code=2,
        stderr="measurement failed",
        elapsed_seconds=0.1,
    )
    runner = QueueRunner([_success(), _success(), failed, _success()])

    with pytest.raises(RuntimeError, match="measurement failed"):
        PhasedPressureMeasurementAdapter(protocol, runner)(5)

    assert runner.calls[-1][0] == "cleanup"


def test_cleanup_failure_overrides_an_untrusted_measurement() -> None:
    protocol = StandardPressureProtocol.model_validate(_payload())
    cleanup_failed = CommandResult(
        status=OperationStatus.FAILED,
        exit_code=3,
        stderr="pressure process remains",
        elapsed_seconds=0.1,
    )
    runner = QueueRunner([_success(), _success(), _success("{}"), cleanup_failed])

    with pytest.raises(RuntimeError, match="pressure process remains"):
        PhasedPressureMeasurementAdapter(protocol, runner)(5)
