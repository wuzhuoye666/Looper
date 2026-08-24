from __future__ import annotations

from datetime import UTC, datetime

import pytest
from looper_core.canonical import canonical_digest
from looper_core.system_opt.collector import (
    CollectedMetric,
    ComponentCollectionPlan,
    ComponentCollectionRequest,
    ComponentCollectionRun,
    ComponentCollectionScope,
    ComponentMetricSnapshot,
    MetricAvailability,
    begin_component_collection,
)
from looper_core.system_opt.dynamic_collection import (
    DynamicCollectionUnavailable,
    O1LiveSource,
    O2ComponentProbe,
    O2ComponentProbeEvidence,
    o1_live_source,
    o2_component_probe,
)
from looper_core.system_opt.hypothesis import ComponentHypothesis
from looper_core.system_opt.observation import O0Observation, ObservationWindow
from looper_core.system_opt.workload import LoadCommandIdentity

ENVIRONMENT = "sha256:" + "e" * 64
WORKLOAD = "sha256:" + "c" * 64
AT = datetime(2026, 8, 24, 1, 0, tzinfo=UTC)


def _plan(component: str) -> ComponentCollectionPlan:
    metrics = {
        "cpu": ["cpu.utilization"],
        "memory": ["memory.used-bytes"],
        "network": ["network.rx-bytes-per-second"],
    }
    scope = (
        ComponentCollectionScope(network_interfaces=["eth0"])
        if component == "network"
        else ComponentCollectionScope()
    )
    return ComponentCollectionPlan(
        component=component,
        target_id="fixture-target",
        environment_digest=ENVIRONMENT,
        workload_phase_id="steady",
        workload_source="external fixture load",
        collector_id=f"fixture.{component}",
        requested_metrics=metrics[component],
        interval_seconds=0.1,
        scope=scope,
    )


class _Session:
    def __init__(self, plan, events: list[str], *, fail_finish: bool = False) -> None:
        self.plan = plan
        self.events = events
        self.fail_finish = fail_finish
        self.closed = False

    def finish(self, request):
        self.events.append(f"finish:{self.plan.component}")
        self.closed = True
        if self.fail_finish:
            raise RuntimeError("fixture finish failed")
        metric_name = self.plan.requested_metrics[0]
        return ComponentMetricSnapshot(
            component=self.plan.component,
            target_id=self.plan.target_id,
            environment_digest=self.plan.environment_digest,
            collected_at=AT,
            metrics={
                metric_name: CollectedMetric(
                    name=metric_name,
                    unit="ratio",
                    value=0.5,
                    availability=MetricAvailability.READABLE,
                    source="fixture live counter",
                )
            },
            counting_basis="fixture exact live window",
        )

    def cancel(self) -> None:
        if not self.closed:
            self.events.append(f"cancel:{self.plan.component}")
            self.closed = True


class _Collector:
    collector_version = "1.0.0"

    def __init__(self, component: str, events: list[str], *, fail_finish: bool = False) -> None:
        self.collector_id = f"fixture.{component}"
        self.events = events
        self.fail_finish = fail_finish

    def begin_collection(self, plan):
        self.events.append(f"begin:{plan.component}")
        return _Session(plan, self.events, fail_finish=self.fail_finish)


def _collect_enabled_fixture_run(
    plan: ComponentCollectionPlan, events: list[str]
) -> ComponentCollectionRun:
    collector = _Collector(plan.component, events)
    opened = begin_component_collection(
        plan, collector=collector, enabled=True, wall_clock=lambda: AT
    )
    request = ComponentCollectionRequest(
        component=plan.component,
        target_id=plan.target_id,
        environment_digest=plan.environment_digest,
        workload_phase_id=plan.workload_phase_id,
        workload_source=plan.workload_source,
        collector_id=plan.collector_id,
        requested_metrics=plan.requested_metrics,
        input_artifacts=[],
        gate_values={},
        interval_seconds=plan.interval_seconds,
        scope=plan.scope,
        measurement_identity={"window_id": "legacy", "observation_layer": "O2"},
    )
    return opened.finish(request)


def _observation_window() -> ObservationWindow:
    return ObservationWindow(
        window_id="window-1",
        phase_id="steady",
        workload_contract_digest=WORKLOAD,
        load_command=LoadCommandIdentity(
            tool="fixture-load",
            argv_digest="sha256:" + "a" * 64,
            declared_duration_seconds=10,
            description="external fixture load",
        ),
        o0=[
            O0Observation(
                metric_id="workload.rate",
                values=[1.0],
                raw_output_digest="sha256:" + "b" * 64,
            )
        ],
        o1=[],
        started_at=AT,
        finished_at=AT,
    )


def test_o1_opens_every_component_before_one_explicit_window_then_finishes() -> None:
    events: list[str] = []
    plans = [_plan("cpu"), _plan("memory")]
    source = o1_live_source(
        plans=plans,
        collectors={
            "cpu": _Collector("cpu", events),
            "memory": _Collector("memory", events),
        },
        window_seconds=2.5,
        sleep_fn=lambda seconds: events.append(f"sleep:{seconds}"),
        wall_clock=lambda: AT,
    )

    assert isinstance(source, O1LiveSource)
    snapshots = source("window-1")

    assert events == [
        "begin:cpu",
        "begin:memory",
        "sleep:2.5",
        "finish:cpu",
        "finish:memory",
    ]
    assert [snapshot.component for snapshot in snapshots] == ["cpu", "memory"]
    assert source.runs_by_window["window-1"][0].request.measurement_identity == {
        "window_id": "window-1",
        "observation_layer": "O1",
    }


def test_o1_returns_none_without_starting_when_any_declared_collector_is_unavailable() -> None:
    events: list[str] = []

    source = o1_live_source(
        plans=[_plan("cpu"), _plan("memory")],
        collectors={"cpu": _Collector("cpu", events), "memory": None},
        window_seconds=1.0,
        sleep_fn=lambda _: events.append("sleep"),
    )

    assert source is None
    assert events == []


def test_o1_runtime_failure_cancels_other_open_component_sessions() -> None:
    events: list[str] = []
    source = o1_live_source(
        plans=[_plan("cpu"), _plan("memory")],
        collectors={
            "cpu": _Collector("cpu", events, fail_finish=True),
            "memory": _Collector("memory", events),
        },
        window_seconds=1.0,
        sleep_fn=lambda _: events.append("sleep"),
        wall_clock=lambda: AT,
    )
    assert source is not None

    with pytest.raises(RuntimeError, match="fixture finish failed"):
        source("window-1")

    assert events == [
        "begin:cpu",
        "begin:memory",
        "sleep",
        "finish:cpu",
        "cancel:memory",
    ]
    assert source.runs_by_window == {}


def test_o2_collects_only_the_routed_component_and_returns_bound_evidence_digest() -> None:
    events: list[str] = []
    monotonic_values = iter([10.0, 10.5, 20.0, 20.75])
    probe = o2_component_probe(
        plans=[_plan("cpu"), _plan("memory")],
        collectors={
            "cpu": _Collector("cpu", events),
            "memory": _Collector("memory", events),
        },
        window_seconds=0.5,
        sleep_fn=lambda seconds: events.append(f"sleep:{seconds}"),
        wall_clock=lambda: AT,
        monotonic=lambda: next(monotonic_values),
    )
    assert isinstance(probe, O2ComponentProbe)
    hypothesis = ComponentHypothesis(
        hypothesis_id="hyp-memory",
        symptom_id="symptom-1",
        component="memory",
        rank=1,
    )
    window = _observation_window()

    digest = probe(hypothesis, window)

    assert events == ["sleep:0.5", "begin:memory", "sleep:0.5", "finish:memory"]
    evidence = probe.evidence_by_digest[digest]
    assert evidence.digest == digest
    assert evidence.hypothesis == hypothesis
    assert evidence.observation_window_digest == window.digest
    assert evidence.collection_run.snapshot is not None
    assert evidence.collection_run.snapshot.component == "memory"
    assert evidence.collection_run.request.measurement_identity == {
        "window_id": "window-1",
        "observation_layer": "O2",
        "hypothesis_digest": hypothesis.digest,
        "observation_window_digest": window.digest,
    }
    overhead_digest = evidence.collection_overhead_evidence_digest
    assert overhead_digest is not None
    overhead = probe.overhead_evidence_by_digest[overhead_digest]
    assert overhead.digest == overhead_digest
    assert overhead.collection_disabled_seconds == [0.5]
    assert overhead.collection_enabled_seconds == [0.75]
    assert overhead.collector_id == "fixture.memory"
    assert "threshold" not in type(overhead).model_fields
    assert "accepted" not in type(overhead).model_fields


def test_o2_evidence_remains_compatible_without_overhead_binding() -> None:
    events: list[str] = []
    plan = _plan("cpu")
    run = _collect_enabled_fixture_run(plan, events)
    hypothesis = ComponentHypothesis(
        hypothesis_id="hyp-cpu",
        symptom_id="symptom-1",
        component="cpu",
        rank=1,
    )
    legacy_payload = {
        "schema_version": "looper.o2-component-probe-evidence/v1alpha1",
        "hypothesis": hypothesis.model_dump(mode="json"),
        "observation_window_digest": _observation_window().digest,
        "collection_run": run.model_dump(mode="json"),
    }

    legacy_digest = canonical_digest(legacy_payload)
    loaded = O2ComponentProbeEvidence.model_validate(legacy_payload)

    assert loaded.collection_overhead_evidence_digest is None
    assert loaded.digest == legacy_digest


def test_o2_failure_does_not_publish_partial_overhead_or_probe_evidence() -> None:
    events: list[str] = []
    probe = o2_component_probe(
        plans=[_plan("cpu")],
        collectors={"cpu": _Collector("cpu", events, fail_finish=True)},
        window_seconds=0.25,
        sleep_fn=lambda seconds: events.append(f"sleep:{seconds}"),
        wall_clock=lambda: AT,
        monotonic=iter([1.0, 1.25, 2.0]).__next__,
    )
    assert probe is not None
    hypothesis = ComponentHypothesis(
        hypothesis_id="hyp-cpu",
        symptom_id="symptom-1",
        component="cpu",
        rank=1,
    )

    with pytest.raises(RuntimeError, match="fixture finish failed"):
        probe(hypothesis, _observation_window())

    assert events == [
        "sleep:0.25",
        "begin:cpu",
        "sleep:0.25",
        "finish:cpu",
    ]
    assert probe.evidence_by_digest == {}
    assert probe.overhead_evidence_by_digest == {}


def test_o2_returns_none_when_declared_probe_capability_is_unavailable() -> None:
    probe = o2_component_probe(
        plans=[_plan("cpu")],
        collectors={"cpu": None},
        window_seconds=1.0,
        sleep_fn=lambda _: None,
    )

    assert probe is None


def test_o2_fails_closed_when_routed_component_has_no_declared_plan() -> None:
    probe = o2_component_probe(
        plans=[_plan("cpu")],
        collectors={"cpu": _Collector("cpu", [])},
        window_seconds=1.0,
        sleep_fn=lambda _: None,
    )
    assert probe is not None
    hypothesis = ComponentHypothesis(
        hypothesis_id="hyp-storage",
        symptom_id="symptom-1",
        component="storage",
        rank=1,
    )

    with pytest.raises(DynamicCollectionUnavailable, match="routed component 'storage'"):
        probe(hypothesis, _observation_window())

    assert probe.evidence_by_digest == {}


@pytest.mark.parametrize("window_seconds", [0.0, -1.0, float("nan"), float("inf")])
def test_live_adapters_reject_implicit_or_invalid_window_duration(window_seconds: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        o1_live_source(
            plans=[_plan("cpu")],
            collectors={"cpu": _Collector("cpu", [])},
            window_seconds=window_seconds,
        )
