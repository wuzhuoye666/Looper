"""Live L4 adapters for the O1/O2 callbacks of ``run_dynamic_phase``.

The module only adapts the existing windowed collector boundary.  It neither
starts the external workload nor changes dynamic-loop routing policy.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Literal

from pydantic import Field, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.collector import (
    CollectionOverheadABEvidence,
    ComponentCollectionPlan,
    ComponentCollectionRequest,
    ComponentCollectionRun,
    ComponentCollectionWindow,
    ComponentMetricSnapshot,
    ComponentName,
    WindowedComponentCollector,
    begin_component_collection,
)
from looper_core.system_opt.hypothesis import ComponentHypothesis
from looper_core.system_opt.observation import ObservationWindow
from looper_core.system_opt.pressure import build_collection_overhead_evidence

O2_COMPONENT_PROBE_EVIDENCE_SCHEMA = "looper.o2-component-probe-evidence/v1alpha1"
_DIGEST = r"^sha256:[0-9a-f]{64}$"


class DynamicCollectionUnavailable(RuntimeError):
    """A routed component has no declared live collection capability."""


class O2ComponentProbeEvidence(StrictModel):
    """Auditable O2 artifact; its digest is the callback result consumed by D2."""

    schema_version: Literal[O2_COMPONENT_PROBE_EVIDENCE_SCHEMA] = (
        O2_COMPONENT_PROBE_EVIDENCE_SCHEMA
    )
    hypothesis: ComponentHypothesis
    observation_window_digest: str = Field(pattern=_DIGEST)
    collection_run: ComponentCollectionRun
    collection_overhead_evidence_digest: str | None = Field(default=None, pattern=_DIGEST)

    @model_validator(mode="after")
    def validate_bindings(self) -> O2ComponentProbeEvidence:
        if self.collection_run.snapshot is None:
            raise ValueError("O2 probe evidence requires an enabled collection snapshot")
        if self.collection_run.request.component != self.hypothesis.component.value:
            raise ValueError("O2 collection component does not match the routed hypothesis")
        return self

    @property
    def digest(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=False)
        if self.collection_overhead_evidence_digest is None:
            payload.pop("collection_overhead_evidence_digest")
        return canonical_digest(payload)


def _validate_window_seconds(window_seconds: float) -> None:
    if not isfinite(window_seconds) or window_seconds <= 0:
        raise ValueError("live collection window_seconds must be finite and positive")


def _index_plans(
    plans: Sequence[ComponentCollectionPlan],
) -> dict[ComponentName, ComponentCollectionPlan]:
    indexed: dict[ComponentName, ComponentCollectionPlan] = {}
    for plan in plans:
        if plan.component in indexed:
            raise ValueError(f"duplicate live collection plan for component {plan.component!r}")
        indexed[plan.component] = plan
    return indexed


def _resolve_collectors(
    plans: Mapping[ComponentName, ComponentCollectionPlan],
    collectors: Mapping[ComponentName, WindowedComponentCollector | None],
) -> dict[ComponentName, WindowedComponentCollector] | None:
    resolved: dict[ComponentName, WindowedComponentCollector] = {}
    for component, plan in plans.items():
        collector = collectors.get(component)
        if collector is None:
            return None
        if collector.collector_id != plan.collector_id:
            raise ValueError(
                f"collector_id mismatch for {component!r}: "
                f"plan={plan.collector_id!r}, collector={collector.collector_id!r}"
            )
        resolved[component] = collector
    return resolved


def _request(
    plan: ComponentCollectionPlan,
    *,
    window_id: str,
    observation_layer: Literal["O1", "O2"],
    identity_bindings: Mapping[str, str] | None = None,
) -> ComponentCollectionRequest:
    measurement_identity = {
        "window_id": window_id,
        "observation_layer": observation_layer,
    }
    measurement_identity.update(identity_bindings or {})
    return ComponentCollectionRequest(
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
        measurement_identity=measurement_identity,
    )


def _cancel_all(windows: Sequence[ComponentCollectionWindow]) -> None:
    first_error: Exception | None = None
    for window in reversed(windows):
        try:
            window.cancel()
        except Exception as error:  # preserve the primary collection failure
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


def _begin_all(
    plans: Sequence[ComponentCollectionPlan],
    collectors: Mapping[ComponentName, WindowedComponentCollector],
    *,
    wall_clock: Callable[[], datetime] | None,
) -> list[ComponentCollectionWindow]:
    windows: list[ComponentCollectionWindow] = []
    try:
        for plan in plans:
            windows.append(
                begin_component_collection(
                    plan,
                    collector=collectors[plan.component],
                    enabled=True,
                    wall_clock=wall_clock,
                )
            )
    except Exception as begin_error:
        try:
            _cancel_all(windows)
        except Exception as cancel_error:
            begin_error.add_note(
                "live collection cancellation also failed: "
                f"{type(cancel_error).__name__}: {cancel_error}"
            )
        raise
    return windows


def _collect_window(
    plans: Sequence[ComponentCollectionPlan],
    collectors: Mapping[ComponentName, WindowedComponentCollector],
    *,
    window_id: str,
    observation_layer: Literal["O1", "O2"],
    window_seconds: float,
    sleep_fn: Callable[[float], None],
    wall_clock: Callable[[], datetime] | None,
    identity_bindings: Mapping[str, str] | None = None,
) -> list[ComponentCollectionRun]:
    windows = _begin_all(plans, collectors, wall_clock=wall_clock)
    try:
        sleep_fn(window_seconds)
        runs: list[ComponentCollectionRun] = []
        for plan, window in zip(plans, windows, strict=True):
            runs.append(
                window.finish(
                    _request(
                        plan,
                        window_id=window_id,
                        observation_layer=observation_layer,
                        identity_bindings=identity_bindings,
                    )
                )
            )
        return runs
    except Exception as collection_error:
        try:
            _cancel_all(windows)
        except Exception as cancel_error:
            collection_error.add_note(
                "live collection cancellation also failed: "
                f"{type(cancel_error).__name__}: {cancel_error}"
            )
        raise


class O1LiveSource:
    """Callable O1 source retaining full L4 runs for audit outside DynamicPhaseRun."""

    def __init__(
        self,
        *,
        plans: Sequence[ComponentCollectionPlan],
        collectors: Mapping[ComponentName, WindowedComponentCollector],
        window_seconds: float,
        sleep_fn: Callable[[float], None],
        wall_clock: Callable[[], datetime] | None,
    ) -> None:
        self._plans = tuple(plans)
        self._collectors = dict(collectors)
        self._window_seconds = window_seconds
        self._sleep_fn = sleep_fn
        self._wall_clock = wall_clock
        self.runs_by_window: dict[str, list[ComponentCollectionRun]] = {}

    def __call__(self, window_id: str) -> list[ComponentMetricSnapshot]:
        if window_id in self.runs_by_window:
            raise ValueError(f"O1 window {window_id!r} was already collected")
        runs = _collect_window(
            self._plans,
            self._collectors,
            window_id=window_id,
            observation_layer="O1",
            window_seconds=self._window_seconds,
            sleep_fn=self._sleep_fn,
            wall_clock=self._wall_clock,
        )
        snapshots = [run.snapshot for run in runs]
        if any(snapshot is None for snapshot in snapshots):
            raise RuntimeError("enabled O1 collection unexpectedly produced no snapshot")
        self.runs_by_window[window_id] = runs
        return [snapshot for snapshot in snapshots if snapshot is not None]


@dataclass(frozen=True)
class _O2OverheadRun:
    protocol_id: str
    protocol_digest: str
    collection_run: ComponentCollectionRun
    elapsed_seconds: float


class O2ComponentProbe:
    """Callable component-routed O2 probe retaining digest-addressed evidence.

    Every routed O2 window is measured as one adjacent A/B pair.  The disabled
    arm first waits for the requested window without entering collector code;
    the enabled arm then runs ``begin_collection -> same wait -> finish`` for
    the same component, plan, target, environment, and O2 window identity.
    Pair order is deliberately fixed as disabled then enabled, so the evidence
    records paired raw wall-clock durations but does not claim to remove serial
    time drift.  No overhead threshold, acceptance verdict, or O2 authorization
    is derived here; those remain upper-layer policy decisions.
    """

    def __init__(
        self,
        *,
        plans: Mapping[ComponentName, ComponentCollectionPlan],
        collectors: Mapping[ComponentName, WindowedComponentCollector],
        window_seconds: float,
        sleep_fn: Callable[[float], None],
        wall_clock: Callable[[], datetime] | None,
        monotonic: Callable[[], float],
    ) -> None:
        self._plans = dict(plans)
        self._collectors = dict(collectors)
        self._window_seconds = window_seconds
        self._sleep_fn = sleep_fn
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self.evidence_by_digest: dict[str, O2ComponentProbeEvidence] = {}
        self.overhead_evidence_by_digest: dict[str, CollectionOverheadABEvidence] = {}

    def __call__(
        self, hypothesis: ComponentHypothesis, window: ObservationWindow
    ) -> str:
        component = hypothesis.component.value
        plan = self._plans.get(component)  # type: ignore[arg-type]
        if plan is None:
            raise DynamicCollectionUnavailable(
                f"no O2 live collection plan for routed component {component!r}"
            )
        identity_bindings = {
            "hypothesis_digest": hypothesis.digest,
            "observation_window_digest": window.digest,
        }
        request = _request(
            plan,
            window_id=window.window_id,
            observation_layer="O2",
            identity_bindings=identity_bindings,
        )
        collector = self._collectors[plan.component]
        protocol_id = "o2-component-probe-overhead-ab/v1alpha1"
        protocol_digest = canonical_digest(
            {
                "protocol_id": protocol_id,
                "pair_order": ["disabled", "enabled"],
                "window_seconds": self._window_seconds,
                "collection_plan_digest": plan.digest,
            }
        )

        disabled_started = self._monotonic()
        disabled_window = begin_component_collection(
            plan,
            collector=collector,
            enabled=False,
            wall_clock=self._wall_clock,
        )
        try:
            self._sleep_fn(self._window_seconds)
            disabled_run = disabled_window.finish(request)
        except Exception:
            disabled_window.cancel()
            raise
        disabled_elapsed = self._monotonic() - disabled_started

        enabled_started = self._monotonic()
        run = _collect_window(
            [plan],
            self._collectors,
            window_id=window.window_id,
            observation_layer="O2",
            window_seconds=self._window_seconds,
            sleep_fn=self._sleep_fn,
            wall_clock=self._wall_clock,
            identity_bindings=identity_bindings,
        )[0]
        enabled_elapsed = self._monotonic() - enabled_started
        overhead = build_collection_overhead_evidence(
            [
                _O2OverheadRun(
                    protocol_id=protocol_id,
                    protocol_digest=protocol_digest,
                    collection_run=disabled_run,
                    elapsed_seconds=disabled_elapsed,
                )
            ],
            [
                _O2OverheadRun(
                    protocol_id=protocol_id,
                    protocol_digest=protocol_digest,
                    collection_run=run,
                    elapsed_seconds=enabled_elapsed,
                )
            ],
            collected_at=(self._wall_clock or (lambda: datetime.now(UTC)))(),
        )
        evidence = O2ComponentProbeEvidence(
            hypothesis=hypothesis,
            observation_window_digest=window.digest,
            collection_run=run,
            collection_overhead_evidence_digest=overhead.digest,
        )
        digest = evidence.digest
        self.overhead_evidence_by_digest[overhead.digest] = overhead
        self.evidence_by_digest[digest] = evidence
        return digest


def o1_live_source(
    *,
    plans: Sequence[ComponentCollectionPlan],
    collectors: Mapping[ComponentName, WindowedComponentCollector | None],
    window_seconds: float,
    sleep_fn: Callable[[float], None] = time.sleep,
    wall_clock: Callable[[], datetime] | None = None,
) -> O1LiveSource | None:
    """Build O1 callback, or return ``None`` when any declared collector is unavailable."""

    _validate_window_seconds(window_seconds)
    indexed = _index_plans(plans)
    if not indexed:
        return None
    resolved = _resolve_collectors(indexed, collectors)
    if resolved is None:
        return None
    ordered_plans = [indexed[component] for component in indexed]
    return O1LiveSource(
        plans=ordered_plans,
        collectors=resolved,
        window_seconds=window_seconds,
        sleep_fn=sleep_fn,
        wall_clock=wall_clock,
    )


def o2_component_probe(
    *,
    plans: Sequence[ComponentCollectionPlan],
    collectors: Mapping[ComponentName, WindowedComponentCollector | None],
    window_seconds: float,
    sleep_fn: Callable[[float], None] = time.sleep,
    wall_clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] = time.perf_counter,
) -> O2ComponentProbe | None:
    """Build routed O2 callback, or ``None`` when declared capability is unavailable."""

    _validate_window_seconds(window_seconds)
    indexed = _index_plans(plans)
    if not indexed:
        return None
    resolved = _resolve_collectors(indexed, collectors)
    if resolved is None:
        return None
    return O2ComponentProbe(
        plans=indexed,
        collectors=resolved,
        window_seconds=window_seconds,
        sleep_fn=sleep_fn,
        wall_clock=wall_clock,
        monotonic=monotonic,
    )


__all__ = [
    "O2_COMPONENT_PROBE_EVIDENCE_SCHEMA",
    "DynamicCollectionUnavailable",
    "O1LiveSource",
    "O2ComponentProbe",
    "O2ComponentProbeEvidence",
    "o1_live_source",
    "o2_component_probe",
]
