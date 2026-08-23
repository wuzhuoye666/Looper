from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from typing import Any

import pytest
from looper_core.system_opt.collector import (
    COLLECTION_BUNDLE_MANIFEST_NAME,
    COLLECTION_BUNDLE_MEDIA_TYPE,
    CollectedMetric,
    CollectionArtifactBundleManifest,
    CollectionArtifactBundleMember,
    ComponentCollectionPlan,
    ComponentCollectionRequest,
    ComponentCollectionScope,
    ComponentMetricSnapshot,
    MetricAvailability,
    begin_component_collection,
)
from looper_core.system_opt.executor import CommandResult, OperationStatus
from looper_core.system_opt.pressure import (
    PhasedPressureCollectionAdapter,
    PressureProtocolError,
    StandardPressureProtocol,
    build_collection_overhead_evidence,
    parse_standard_pressure_protocol_yaml,
)
from pydantic import ValidationError

_RAW_MEMBER = b'{"fixture":"cpu-pressure","samples":[100.0,101.0,99.0]}'
_RAW_MANIFEST = CollectionArtifactBundleManifest(
    members=[
        CollectionArtifactBundleMember(
            path="raw/cpu-tool-output.json",
            media_type="application/vnd.fixture.cpu+json",
            size_bytes=len(_RAW_MEMBER),
            digest="sha256:" + hashlib.sha256(_RAW_MEMBER).hexdigest(),
        )
    ]
)
_RAW_BUFFER = io.BytesIO()
with zipfile.ZipFile(_RAW_BUFFER, "w") as _RAW_ARCHIVE:
    _RAW_ARCHIVE.writestr(
        COLLECTION_BUNDLE_MANIFEST_NAME,
        json.dumps(_RAW_MANIFEST.model_dump(mode="json")),
    )
    _RAW_ARCHIVE.writestr("raw/cpu-tool-output.json", _RAW_MEMBER)
_RAW_ARTIFACT = _RAW_BUFFER.getvalue()
_RAW_DIGEST = _RAW_MANIFEST.digest
_ENVIRONMENT_DIGEST = "sha256:" + "a" * 64


def _collection_payload() -> dict[str, Any]:
    return {
        "schema_version": "looper.standard-pressure-protocol/v1alpha1",
        "id": "cpu-decoupled-v1",
        "component": "cpu",
        "target_scope": "one explicit fixture target",
        "limitation": "fixture evidence only",
        "required_executables": ["prepare", "warmup", "measure", "verify", "cleanup"],
        "input_identity": {"policy_id": "fixture-policy"},
        "metric_ids": ["cpu.score", "cpu.success"],
        "gate_metric_ids": ["cpu.success"],
        "stability": {
            "metric_id": "cpu.score",
            "statistic": "cv",
            "enforcement": "report-only",
            "acceptance_limit": None,
            "minimum_repeats": 3,
            "maximum_repeats": 3,
            "source": "fixture-only contract",
        },
        "collection": {
            "collector_id": "fixture.windowed-cpu",
            "requested_metrics": [
                "cpu.score",
                "cpu.success",
                "cpu.busy-ratio",
                "cpu.pmu-cycles",
            ],
            "artifact_requirements": [
                {
                    "artifact_id": "cpu-tool-output",
                    "media_type": COLLECTION_BUNDLE_MEDIA_TYPE,
                }
            ],
            "interval_seconds": 0.25,
            "scope": {},
            "workload_source": "fixture controlled CPU workload",
        },
        "phases": [
            {
                "id": "prepare",
                "kind": "prepare",
                "command": {"argv": ["prepare", "{repeats}"], "timeout_seconds": 2},
                "declared_duration_seconds": 0,
                "purpose": "freeze fixture input",
            },
            {
                "id": "warmup",
                "kind": "warmup",
                "command": {"argv": ["warmup", "{repeats}"], "timeout_seconds": 3},
                "declared_duration_seconds": 1,
                "purpose": "discard fixture warmup",
            },
            {
                "id": "measure",
                "kind": "measure",
                "command": {"argv": ["measure", "{repeats}"], "timeout_seconds": 4},
                "declared_duration_seconds": 2,
                "purpose": "emit digest-bound pressure artifacts only",
            },
            {
                "id": "verify",
                "kind": "verify",
                "command": {"argv": ["verify", "{repeats}"], "timeout_seconds": 2},
                "declared_duration_seconds": 0,
                "purpose": "verify fixture artifacts",
            },
            {
                "id": "cleanup",
                "kind": "cleanup",
                "command": {"argv": ["cleanup", "{repeats}"], "timeout_seconds": 2},
                "declared_duration_seconds": 0,
                "purpose": "remove fixture workload",
            },
        ],
    }


def _execution_evidence(*, digest: str = _RAW_DIGEST) -> str:
    return json.dumps(
        {
            "schema_version": "looper.pressure-execution-evidence/v1alpha1",
            "protocol_id": "cpu-decoupled-v1",
            "component": "cpu",
            "workload_phase_id": "measure",
            "measurement_identity": {
                "target": "fixture-target",
                "workload": "fixture-cpu-v1",
                "statistics": "three-preserved-observations",
            },
            "artifacts": [
                {
                    "artifact_id": "cpu-tool-output",
                    "source": "fixture://cpu-tool-output",
                    "media_type": COLLECTION_BUNDLE_MEDIA_TYPE,
                    "digest": digest,
                }
            ],
            "gate_values": {"cpu.success": True},
        },
        separators=(",", ":"),
    )


def _success(stdout: str = "", elapsed_seconds: float = 0.1) -> CommandResult:
    return CommandResult(
        status=OperationStatus.SUCCEEDED,
        exit_code=0,
        stdout=stdout,
        elapsed_seconds=elapsed_seconds,
    )


class OrderedRunner:
    def __init__(self, results: list[CommandResult], events: list[str]) -> None:
        self.results = list(results)
        self.events = events
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, timeout_seconds: float) -> CommandResult:
        del timeout_seconds
        self.calls.append(argv)
        self.events.append(f"run:{argv[0]}")
        return self.results.pop(0)


class FixtureSession:
    def __init__(
        self,
        plan: ComponentCollectionPlan,
        events: list[str],
        cancel_error: Exception | None = None,
    ) -> None:
        self.plan = plan
        self.events = events
        self.cancel_error = cancel_error
        self.cancelled = False

    def finish(self, request: ComponentCollectionRequest) -> ComponentMetricSnapshot:
        self.events.append("collector:finish")
        assert request.input_artifacts[0].digest == _RAW_DIGEST
        return ComponentMetricSnapshot(
            component="cpu",
            target_id=request.target_id,
            environment_digest=request.environment_digest,
            collected_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
            metrics={
                "cpu.score": CollectedMetric(
                    name="cpu.score",
                    unit="score/s",
                    value=[100.0, 101.0, 99.0],
                    availability=MetricAvailability.READABLE,
                    source=request.input_artifacts[0].source,
                ),
                "cpu.success": CollectedMetric(
                    name="cpu.success",
                    unit="boolean",
                    value=[1.0, 1.0, 1.0],
                    availability=MetricAvailability.READABLE,
                    source="pressure process exit status",
                ),
                "cpu.busy-ratio": CollectedMetric(
                    name="cpu.busy-ratio",
                    unit="ratio",
                    value=0.95,
                    availability=MetricAvailability.READABLE,
                    source="fixture workload window",
                ),
                "cpu.pmu-cycles": CollectedMetric(
                    name="cpu.pmu-cycles",
                    unit="count",
                    value=None,
                    availability=MetricAvailability.UNAVAILABLE,
                    unavailable_reason="fixture guest does not expose PMU",
                    source="fixture /sys probe",
                ),
            },
            counting_basis="raw tool distribution plus guest facts over the same workload window",
        )

    def cancel(self) -> None:
        self.cancelled = True
        self.events.append("collector:cancel")
        if self.cancel_error is not None:
            raise self.cancel_error


class FixtureWindowCollector:
    collector_id = "fixture.windowed-cpu"
    collector_version = "1.0"

    def __init__(
        self,
        events: list[str],
        *,
        cancel_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.cancel_error = cancel_error
        self.begin_calls = 0
        self.session: FixtureSession | None = None

    def begin_collection(self, plan: ComponentCollectionPlan) -> FixtureSession:
        self.begin_calls += 1
        self.events.append("collector:begin")
        self.session = FixtureSession(plan, self.events, self.cancel_error)
        return self.session


def _adapter(
    *,
    protocol: StandardPressureProtocol,
    runner: OrderedRunner,
    collector: FixtureWindowCollector,
    enabled: bool = True,
    digest: str = _RAW_DIGEST,
) -> PhasedPressureCollectionAdapter:
    times = iter([10.0, 11.0])
    return PhasedPressureCollectionAdapter(
        protocol,
        runner,
        collector=collector,
        target_id="fixture-target",
        environment_digest=_ENVIRONMENT_DIGEST,
        collection_enabled=enabled,
        artifact_reader=lambda artifact: (
            _RAW_ARTIFACT if artifact.digest == digest else b"not-the-declared-artifact"
        ),
        monotonic=lambda: next(times),
        wall_clock=iter(
            [
                datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
                datetime(2026, 8, 23, 12, 0, 1, tzinfo=UTC),
            ]
        ).__next__,
    )


def test_collection_schema_is_additive_and_keeps_legacy_protocol_digest() -> None:
    root = __import__("pathlib").Path(__file__).parents[1] / "examples" / "system-optimizer"
    expected = {
        "cpu-pressure-calibration-protocol.yaml": (
            "sha256:b3f4fbcec53a22b67cb55f91bf9bff56912df9af6b3f4400a125674900f95afd"
        ),
        "memory-pressure-calibration-protocol.yaml": (
            "sha256:bdacf0baafbb42d53a1333d8bec0269b7fa3be17693afe30583a79c84abea451"
        ),
        "network-loopback-calibration-protocol.yaml": (
            "sha256:19c0676401673cacb3a9380b0d05405081a6c25433ed5e2956031dd68d692894"
        ),
    }

    for filename, digest in expected.items():
        protocol = parse_standard_pressure_protocol_yaml(
            (root / filename).read_text(encoding="utf-8")
        )
        assert protocol.collection is None
        assert protocol.digest == digest


def test_collection_contract_requires_explicit_component_metrics_and_artifacts() -> None:
    payload = _collection_payload()
    payload["collection"]["requested_metrics"] = ["workload.score"]

    with pytest.raises(ValidationError, match="requested_metrics.*component"):
        StandardPressureProtocol.model_validate(payload)

    payload = _collection_payload()
    payload["collection"]["artifact_requirements"] = []
    with pytest.raises(ValidationError, match="artifact_requirements"):
        StandardPressureProtocol.model_validate(payload)


def test_network_collection_contract_requires_an_explicit_interface() -> None:
    payload = _collection_payload()
    payload["component"] = "network"
    payload["metric_ids"] = ["network.score", "network.success"]
    payload["gate_metric_ids"] = ["network.success"]
    payload["stability"]["metric_id"] = "network.score"
    payload["collection"]["requested_metrics"] = ["network.score", "network.success"]

    with pytest.raises(ValidationError, match="network_interfaces"):
        StandardPressureProtocol.model_validate(payload)


def test_windowed_adapter_collects_during_measure_and_binds_all_evidence() -> None:
    protocol = StandardPressureProtocol.model_validate(_collection_payload())
    events: list[str] = []
    runner = OrderedRunner(
        [_success(), _success(), _success(_execution_evidence()), _success(), _success()],
        events,
    )
    collector = FixtureWindowCollector(events)

    result = _adapter(protocol=protocol, runner=runner, collector=collector)(3)

    assert events == [
        "run:prepare",
        "run:warmup",
        "collector:begin",
        "run:measure",
        "collector:finish",
        "run:verify",
        "run:cleanup",
    ]
    assert result.collection_run.enabled is True
    assert result.envelope is not None
    batch = result.envelope.measurement_batch
    assert batch.metrics["cpu.score"].values == [100.0, 101.0, 99.0]
    assert batch.metrics["cpu.busy-ratio"].values == [0.95]
    assert "cpu.pmu-cycles" not in batch.metrics
    assert result.envelope.unavailable_metrics["cpu.pmu-cycles"].unavailable_reason
    assert batch.gate_values == {"cpu.success": True}
    assert batch.pressure_protocol_digest == protocol.digest
    assert [phase.kind for phase in batch.phase_evidence] == [
        "prepare",
        "warmup",
        "measure",
        "verify",
        "cleanup",
    ]
    assert batch.stability_evidence is not None
    assert result.elapsed_seconds == 1.0


def test_disabled_collection_runs_the_same_pressure_but_never_calls_collector() -> None:
    protocol = StandardPressureProtocol.model_validate(_collection_payload())
    events: list[str] = []
    runner = OrderedRunner(
        [_success(), _success(), _success(_execution_evidence()), _success(), _success()],
        events,
    )
    collector = FixtureWindowCollector(events)

    result = _adapter(
        protocol=protocol,
        runner=runner,
        collector=collector,
        enabled=False,
    )(3)

    assert collector.begin_calls == 0
    assert result.collection_run.enabled is False
    assert result.envelope is None
    assert result.execution_evidence.artifacts[0].digest == _RAW_DIGEST
    assert [call[0] for call in runner.calls] == [
        "prepare",
        "warmup",
        "measure",
        "verify",
        "cleanup",
    ]


def test_artifact_digest_mismatch_fails_closed_cancels_window_and_still_cleans_up() -> None:
    protocol = StandardPressureProtocol.model_validate(_collection_payload())
    events: list[str] = []
    wrong_digest = "sha256:" + "b" * 64
    runner = OrderedRunner(
        [_success(), _success(), _success(_execution_evidence(digest=wrong_digest)), _success()],
        events,
    )
    collector = FixtureWindowCollector(events)

    with pytest.raises(PressureProtocolError, match="artifact digest"):
        _adapter(
            protocol=protocol,
            runner=runner,
            collector=collector,
            digest=wrong_digest,
        )(3)

    assert collector.session is not None and collector.session.cancelled is True
    assert runner.calls[-1][0] == "cleanup"
    assert "run:verify" not in events


def test_cancel_failure_does_not_skip_cleanup_or_mask_the_primary_failure() -> None:
    protocol = StandardPressureProtocol.model_validate(_collection_payload())
    events: list[str] = []
    failed_measure = CommandResult(
        status=OperationStatus.FAILED,
        exit_code=2,
        stderr="pressure measure failed",
        elapsed_seconds=0.1,
    )
    runner = OrderedRunner([_success(), _success(), failed_measure, _success()], events)
    collector = FixtureWindowCollector(
        events,
        cancel_error=RuntimeError("collector cancel failed"),
    )

    with pytest.raises(RuntimeError, match="pressure measure failed") as captured:
        _adapter(protocol=protocol, runner=runner, collector=collector)(3)

    assert runner.calls[-1][0] == "cleanup"
    assert collector.session is not None and collector.session.cancelled is True
    assert any("collector cancel failed" in note for note in captured.value.__notes__)


def test_cleanup_failure_overrides_measure_and_cancel_failures() -> None:
    protocol = StandardPressureProtocol.model_validate(_collection_payload())
    events: list[str] = []
    failed_measure = CommandResult(
        status=OperationStatus.FAILED,
        exit_code=2,
        stderr="pressure measure failed",
        elapsed_seconds=0.1,
    )
    failed_cleanup = CommandResult(
        status=OperationStatus.FAILED,
        exit_code=3,
        stderr="pressure process remains",
        elapsed_seconds=0.1,
    )
    runner = OrderedRunner([_success(), _success(), failed_measure, failed_cleanup], events)
    collector = FixtureWindowCollector(
        events,
        cancel_error=RuntimeError("collector cancel failed"),
    )

    with pytest.raises(RuntimeError, match="pressure process remains"):
        _adapter(protocol=protocol, runner=runner, collector=collector)(3)

    assert runner.calls[-1][0] == "cleanup"


def test_actual_collector_identity_must_match_the_protocol_before_pressure_runs() -> None:
    protocol = StandardPressureProtocol.model_validate(_collection_payload())
    events: list[str] = []
    runner = OrderedRunner([], events)
    collector = FixtureWindowCollector(events)
    collector.collector_id = "fixture.unrequested-collector"

    with pytest.raises(ValueError, match="collector_id"):
        _adapter(protocol=protocol, runner=runner, collector=collector)(3)

    assert runner.calls == []


def test_result_rejects_execution_identity_tampering_after_collection() -> None:
    protocol = StandardPressureProtocol.model_validate(_collection_payload())
    events: list[str] = []
    runner = OrderedRunner(
        [_success(), _success(), _success(_execution_evidence()), _success(), _success()],
        events,
    )
    collector = FixtureWindowCollector(events)
    result = _adapter(protocol=protocol, runner=runner, collector=collector)(3)
    payload = result.model_dump(mode="python")
    payload["execution_evidence"]["measurement_identity"]["run"] = "tampered-run"

    with pytest.raises(ValidationError, match="request identity.*execution"):
        type(result).model_validate(payload)


def test_collection_overhead_evidence_keeps_raw_pairs_without_a_threshold() -> None:
    protocol = StandardPressureProtocol.model_validate(_collection_payload())

    def run_one(enabled: bool, elapsed: float):
        events: list[str] = []
        runner = OrderedRunner(
            [_success(), _success(), _success(_execution_evidence()), _success(), _success()],
            events,
        )
        collector = FixtureWindowCollector(events)
        times = iter([20.0, 20.0 + elapsed])
        adapter = PhasedPressureCollectionAdapter(
            protocol,
            runner,
            collector=collector,
            target_id="fixture-target",
            environment_digest=_ENVIRONMENT_DIGEST,
            collection_enabled=enabled,
            artifact_reader=lambda artifact: _RAW_ARTIFACT,
            monotonic=lambda: next(times),
            wall_clock=iter(
                [
                    datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
                    datetime(2026, 8, 23, 12, 0, 1, tzinfo=UTC),
                ]
            ).__next__,
        )
        return adapter(3)

    disabled = [run_one(False, 2.0), run_one(False, 2.1)]
    enabled = [run_one(True, 2.2), run_one(True, 2.3)]
    evidence = build_collection_overhead_evidence(
        disabled,
        enabled,
        collected_at=datetime(2026, 8, 23, 12, 5, tzinfo=UTC),
    )

    assert evidence.collection_disabled_seconds == [2.0, pytest.approx(2.1)]
    assert evidence.collection_enabled_seconds == [pytest.approx(2.2), pytest.approx(2.3)]
    assert "threshold" not in type(evidence).model_fields
    assert "accepted" not in type(evidence).model_fields


def test_collection_window_rejects_request_identity_drift() -> None:
    plan = ComponentCollectionPlan(
        component="cpu",
        target_id="fixture-target",
        environment_digest=_ENVIRONMENT_DIGEST,
        workload_phase_id="measure",
        workload_source="fixture",
        collector_id="fixture.windowed-cpu",
        requested_metrics=["cpu.score"],
        interval_seconds=0.25,
        scope=ComponentCollectionScope(),
    )
    events: list[str] = []
    collector = FixtureWindowCollector(events)
    window = begin_component_collection(
        plan,
        collector=collector,
        enabled=True,
        wall_clock=lambda: datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
    )
    drifted_request = ComponentCollectionRequest(
        component=plan.component,
        target_id="different-target",
        environment_digest=plan.environment_digest,
        workload_phase_id=plan.workload_phase_id,
        workload_source=plan.workload_source,
        collector_id=plan.collector_id,
        requested_metrics=plan.requested_metrics,
        input_artifacts=[],
        interval_seconds=plan.interval_seconds,
        scope=plan.scope,
        measurement_identity={"run_id": "fixture-run"},
    )

    with pytest.raises(ValueError, match="does not match.*plan"):
        window.finish(drifted_request)

    assert collector.session is not None and collector.session.cancelled is True
    assert events == ["collector:begin", "collector:cancel"]
