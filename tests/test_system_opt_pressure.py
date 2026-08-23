from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from looper_core.system_opt.demo import build_demo_policy
from looper_core.system_opt.executor import CommandResult, OperationStatus
from looper_core.system_opt.policy import OptimizationMode, parse_optimization_policy_yaml
from looper_core.system_opt.pressure import (
    PhasedPressureMeasurementAdapter,
    PressureProtocolError,
    StandardPressureProtocol,
    calibrate_cv_acceptance_limit,
    evaluate_measurement_stability,
    parse_standard_pressure_protocol_yaml,
    validate_pressure_policy,
)
from looper_core.system_opt.scoring import MeasurementBatch
from looper_core.system_opt.tuning import OptimizationRun
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


def test_cv_limit_bootstrap_uses_one_shared_resample_for_mean_and_stdev() -> None:
    batch = MeasurementBatch.model_validate(
        {
            "identity": {"target": "t"},
            "metrics": {
                "cpu.score": {
                    "metric_id": "cpu.score",
                    "values": [9.0, 10.0, 11.0, 10.0, 9.5, 10.5, 10.0],
                }
            },
            "gate_values": {},
        }
    )

    first = calibrate_cv_acceptance_limit(
        batch,
        "cpu.score",
        confidence_level=0.95,
        bootstrap_resamples=2000,
        random_seed=20260823,
        target_scope="one test target",
        portability="test-only",
    )
    second = calibrate_cv_acceptance_limit(
        batch,
        "cpu.score",
        confidence_level=0.95,
        bootstrap_resamples=2000,
        random_seed=20260823,
        target_scope="one test target",
        portability="test-only",
    )

    assert first == second
    assert first.acceptance_limit == pytest.approx(0.08321378679337135)
    assert first.input_batch_digest == batch.digest


def test_cv_limit_bootstrap_rejects_zero_mean() -> None:
    batch = MeasurementBatch.model_validate(
        {
            "identity": {"target": "t"},
            "metrics": {
                "zero": {"metric_id": "zero", "values": [-1.0, 0.0, 1.0]}
            },
            "gate_values": {},
        }
    )

    with pytest.raises(PressureProtocolError, match="zero mean"):
        calibrate_cv_acceptance_limit(
            batch,
            "zero",
            confidence_level=0.95,
            bootstrap_resamples=100,
            random_seed=1,
            target_scope="one test target",
            portability="test-only",
        )


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


def test_aliyun_memory_capability_digest_binds_the_raw_readback() -> None:
    root = Path(__file__).parents[1] / "examples" / "system-optimizer"
    raw = (root / "aliyun-ecs-memory-thp-capability-raw.txt").read_bytes()
    domains = json.loads(
        (root / "aliyun-ecs-memory-thp-capability-domains.json").read_text(
            encoding="utf-8"
        )
    )

    expected = "sha256:" + hashlib.sha256(raw).hexdigest()
    assert domains[0]["evidence_digest"] == expected, (
        "capability raw evidence bytes do not match the registered digest; "
        "if the actual value is the CRLF variant, this checkout smudged the "
        "evidence file — evidence blobs are byte-faithful (-text, SO-D022); "
        "pull the updated .gitattributes and refresh with git checkout"
    )


def test_aliyun_memory_hard_gate_protocol_binds_the_task_policy() -> None:
    root = Path(__file__).parents[1] / "examples" / "system-optimizer"
    protocol = parse_standard_pressure_protocol_yaml(
        (root / "memory-pressure-hard-gate-protocol.yaml").read_text(encoding="utf-8")
    )
    policy = parse_optimization_policy_yaml(
        (root / "aliyun-ecs-memory-thp-policy.yaml").read_text(encoding="utf-8")
    )

    validate_pressure_policy(protocol, policy)
    assert protocol.stability.enforcement == "hard-gate"
    assert protocol.stability.acceptance_limit == pytest.approx(0.02922585447690954)
    assert [phase.id for phase in protocol.phases] == [
        "exclusive-window",
        "prepare",
        "warmup",
        "measure",
        "exclusive-window-after",
        "verify",
        "cleanup",
    ]


def test_aliyun_memory_acceptance_summary_binds_the_optimization_run() -> None:
    root = (
        Path(__file__).parents[1]
        / ".artifacts"
        / "system-opt"
        / "m2-memory-thp-search-20260823"
    )
    summary = json.loads((root / "acceptance-summary.json").read_text(encoding="utf-8"))
    run = OptimizationRun.model_validate_json(
        (root / "remote-run" / "evidence" / "optimization-run.json").read_text(
            encoding="utf-8"
        )
    )

    assert summary["identities"]["optimization_run_digest"] == run.digest
    assert summary["closed_loop"]["stop_reason"] == run.stop_reason.value
    assert summary["closed_loop"]["recommended_candidate_id"] is None

    primary_metric = summary["closed_loop"]["primary_metric_id"]
    assert (
        summary["closed_loop"]["baseline"]["measurement_batch_digest"] == run.baseline.digest
    )
    assert (
        summary["closed_loop"]["baseline"]["primary_metric_evidence_digest"]
        == run.baseline.metrics[primary_metric].digest
    )
    summary_candidates = summary["closed_loop"]["candidates"]
    assert [candidate["measurement_batch_digest"] for candidate in summary_candidates] == [
        candidate.measurement_digest for candidate in run.candidates
    ]
    for summary_candidate, run_candidate in zip(summary_candidates, run.candidates, strict=True):
        assert (
            summary_candidate["primary_metric_evidence_digest"]
            == run_candidate.improvements[primary_metric].candidate_digest
        )
        assert (
            run_candidate.improvements[primary_metric].baseline_digest
            == summary["closed_loop"]["baseline"]["primary_metric_evidence_digest"]
        )
    assert all(candidate.safety_state.value == "rolled_back" for candidate in run.candidates)


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
