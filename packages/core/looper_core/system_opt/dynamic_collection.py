"""Live L4 adapters for the O1/O2 callbacks of ``run_dynamic_phase``.

The module only adapts the existing windowed collector boundary.  It neither
starts the external workload nor changes dynamic-loop routing policy.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
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
DYNAMIC_COLLECTION_EVIDENCE_INDEX_SCHEMA = (
    "looper.dynamic-collection-evidence-index/v1alpha1"
)
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
    enabled: bool,
    wall_clock: Callable[[], datetime] | None,
) -> list[ComponentCollectionWindow]:
    windows: list[ComponentCollectionWindow] = []
    try:
        for plan in plans:
            windows.append(
                begin_component_collection(
                    plan,
                    collector=collectors[plan.component],
                    enabled=enabled,
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
    enabled: bool = True,
) -> list[ComponentCollectionRun]:
    windows = _begin_all(plans, collectors, enabled=enabled, wall_clock=wall_clock)
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


@dataclass(frozen=True)
class _CollectionOverheadRun:
    protocol_id: str
    protocol_digest: str
    collection_run: ComponentCollectionRun
    elapsed_seconds: float


class O1LiveSource:
    """Callable O1 source retaining runs and bounded session overhead evidence.

    O1 is the always-on coarse observation layer, so per-window A/B would
    double every observation window.  Instead, the first successful session
    window performs one adjacent pair: a disabled arm waits for the requested
    duration without entering collector code, then the normal enabled arm runs
    ``begin all -> same wait -> finish all``.  Later windows run enabled only
    and bind back to that first-window evidence.

    Pair order is fixed as disabled then enabled and therefore does not remove
    serial time drift.  Both raw wall-clock durations cover the whole concurrent
    O1 collector set; the per-collector evidence records set membership, not an
    isolated attribution of total cost to that collector.  No threshold,
    acceptance verdict, or authorization is derived here.
    """

    def __init__(
        self,
        *,
        plans: Sequence[ComponentCollectionPlan],
        collectors: Mapping[ComponentName, WindowedComponentCollector],
        window_seconds: float,
        sleep_fn: Callable[[float], None],
        wall_clock: Callable[[], datetime] | None,
        monotonic: Callable[[], float],
    ) -> None:
        self._plans = tuple(plans)
        self._collectors = dict(collectors)
        self._window_seconds = window_seconds
        self._sleep_fn = sleep_fn
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._session_overhead_digests: dict[ComponentName, str] | None = None
        self.runs_by_window: dict[str, list[ComponentCollectionRun]] = {}
        self.overhead_digests_by_window: dict[str, dict[ComponentName, str]] = {}
        self.overhead_evidence_by_digest: dict[str, CollectionOverheadABEvidence] = {}

    def _collect_arm(
        self, window_id: str, *, enabled: bool
    ) -> tuple[list[ComponentCollectionRun], float]:
        started = self._monotonic()
        runs = _collect_window(
            self._plans,
            self._collectors,
            window_id=window_id,
            observation_layer="O1",
            window_seconds=self._window_seconds,
            sleep_fn=self._sleep_fn,
            wall_clock=self._wall_clock,
            enabled=enabled,
        )
        return runs, self._monotonic() - started

    def _build_first_window_overhead(
        self,
        disabled_runs: Sequence[ComponentCollectionRun],
        disabled_elapsed: float,
        enabled_runs: Sequence[ComponentCollectionRun],
        enabled_elapsed: float,
    ) -> tuple[dict[ComponentName, str], dict[str, CollectionOverheadABEvidence]]:
        protocol_id = "o1-live-source-overhead-ab/v1alpha1"
        protocol_digest = canonical_digest(
            {
                "protocol_id": protocol_id,
                "pair_order": ["disabled", "enabled"],
                "pair_scope": "concurrent-o1-collector-set",
                "window_seconds": self._window_seconds,
                "collection_plan_digests": [plan.digest for plan in self._plans],
            }
        )
        collected_at = (self._wall_clock or (lambda: datetime.now(UTC)))()
        digests: dict[ComponentName, str] = {}
        evidence_by_digest: dict[str, CollectionOverheadABEvidence] = {}
        for disabled_run, enabled_run in zip(disabled_runs, enabled_runs, strict=True):
            overhead = build_collection_overhead_evidence(
                [
                    _CollectionOverheadRun(
                        protocol_id=protocol_id,
                        protocol_digest=protocol_digest,
                        collection_run=disabled_run,
                        elapsed_seconds=disabled_elapsed,
                    )
                ],
                [
                    _CollectionOverheadRun(
                        protocol_id=protocol_id,
                        protocol_digest=protocol_digest,
                        collection_run=enabled_run,
                        elapsed_seconds=enabled_elapsed,
                    )
                ],
                collected_at=collected_at,
            )
            component = enabled_run.request.component
            digests[component] = overhead.digest
            evidence_by_digest[overhead.digest] = overhead
        return digests, evidence_by_digest

    def __call__(self, window_id: str) -> list[ComponentMetricSnapshot]:
        if window_id in self.runs_by_window:
            raise ValueError(f"O1 window {window_id!r} was already collected")

        pending_evidence: dict[str, CollectionOverheadABEvidence] = {}
        if self._session_overhead_digests is None:
            disabled_runs, disabled_elapsed = self._collect_arm(window_id, enabled=False)
            runs, enabled_elapsed = self._collect_arm(window_id, enabled=True)
            overhead_digests, pending_evidence = self._build_first_window_overhead(
                disabled_runs,
                disabled_elapsed,
                runs,
                enabled_elapsed,
            )
        else:
            runs, _ = self._collect_arm(window_id, enabled=True)
            overhead_digests = self._session_overhead_digests

        snapshots = [run.snapshot for run in runs]
        if any(snapshot is None for snapshot in snapshots):
            raise RuntimeError("enabled O1 collection unexpectedly produced no snapshot")
        if self._session_overhead_digests is None:
            self._session_overhead_digests = overhead_digests
            self.overhead_evidence_by_digest.update(pending_evidence)
        self.runs_by_window[window_id] = runs
        self.overhead_digests_by_window[window_id] = dict(overhead_digests)
        return [snapshot for snapshot in snapshots if snapshot is not None]


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
                _CollectionOverheadRun(
                    protocol_id=protocol_id,
                    protocol_digest=protocol_digest,
                    collection_run=disabled_run,
                    elapsed_seconds=disabled_elapsed,
                )
            ],
            [
                _CollectionOverheadRun(
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
    monotonic: Callable[[], float] = time.perf_counter,
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
        monotonic=monotonic,
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


class DynamicCollectionEvidenceIndex(StrictModel):
    """Digest index for replaying live collection evidence written to ``control/``."""

    schema_version: Literal[DYNAMIC_COLLECTION_EVIDENCE_INDEX_SCHEMA] = (
        DYNAMIC_COLLECTION_EVIDENCE_INDEX_SCHEMA
    )
    o1_runs_by_window: dict[str, list[str]]
    o1_overhead_digests_by_window: dict[str, dict[ComponentName, str]]
    o2_probe_evidence_digests: list[str]
    o2_overhead_evidence_digests: list[str]


def _evidence_filename(kind: str, digest: str) -> str:
    if not digest.startswith("sha256:"):
        raise ValueError("collection evidence filename requires a sha256 digest")
    return f"{kind}-{digest.removeprefix('sha256:')}.json"


def _atomic_write_json(path: Path, payload: object) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def persist_dynamic_collection_evidence(
    control_dir: Path,
    *,
    o1_source: O1LiveSource | None = None,
    o2_probe: O2ComponentProbe | None = None,
) -> DynamicCollectionEvidenceIndex:
    """Atomically persist live O1/O2 evidence under a session ``control/`` directory.

    Files use type-prefixed, digest-addressed names, while the fixed index maps
    observation windows to O1 run/overhead digests and lists O2 artifacts.  The
    caller owns CLI lifecycle wiring; this helper neither reads ``windows/`` nor
    changes dynamic-loop behavior.
    """

    if control_dir.name != "control":
        raise ValueError("dynamic collection evidence must be written under control/")
    o1_runs_by_window: dict[str, list[str]] = {}
    o1_overhead_by_window: dict[str, dict[ComponentName, str]] = {}
    if o1_source is not None:
        o1_runs_by_window = {
            window_id: [run.digest for run in runs]
            for window_id, runs in o1_source.runs_by_window.items()
        }
        o1_overhead_by_window = {
            window_id: dict(digests)
            for window_id, digests in o1_source.overhead_digests_by_window.items()
        }
        if set(o1_runs_by_window) != set(o1_overhead_by_window):
            raise ValueError("O1 run windows are not fully bound to overhead evidence")
        for window_id, runs in o1_source.runs_by_window.items():
            run_components = [run.request.component for run in runs]
            if len(run_components) != len(set(run_components)):
                raise ValueError(f"O1 window {window_id!r} repeats a component run")
            if set(run_components) != set(o1_overhead_by_window[window_id]):
                raise ValueError(
                    f"O1 window {window_id!r} components are not exactly bound to overhead evidence"
                )
        referenced_overhead = {
            digest for bindings in o1_overhead_by_window.values() for digest in bindings.values()
        }
        if referenced_overhead != set(o1_source.overhead_evidence_by_digest):
            raise ValueError("O1 retained overhead evidence does not match window bindings")
        for digest, evidence in o1_source.overhead_evidence_by_digest.items():
            if digest != evidence.digest:
                raise ValueError("O1 overhead evidence key does not match its digest")

    o2_probe_digests: list[str] = []
    o2_overhead_digests: list[str] = []
    if o2_probe is not None:
        o2_probe_digests = list(o2_probe.evidence_by_digest)
        o2_overhead_digests = list(o2_probe.overhead_evidence_by_digest)
        referenced_overhead = {
            evidence.collection_overhead_evidence_digest
            for evidence in o2_probe.evidence_by_digest.values()
        }
        if referenced_overhead != set(o2_probe.overhead_evidence_by_digest):
            raise ValueError("O2 retained overhead evidence does not match probe bindings")
        for digest, evidence in o2_probe.evidence_by_digest.items():
            if digest != evidence.digest:
                raise ValueError("O2 probe evidence key does not match its digest")
        for digest, evidence in o2_probe.overhead_evidence_by_digest.items():
            if digest != evidence.digest:
                raise ValueError("O2 overhead evidence key does not match its digest")

    # Validate the complete in-memory graph before creating any on-disk artifacts.
    control_dir.mkdir(parents=True, exist_ok=True)
    if o1_source is not None:
        for runs in o1_source.runs_by_window.values():
            for run in runs:
                _atomic_write_json(
                    control_dir / _evidence_filename("o1-collection-run", run.digest),
                    run.model_dump(mode="json", exclude_none=False),
                )
        for digest, evidence in o1_source.overhead_evidence_by_digest.items():
            _atomic_write_json(
                control_dir / _evidence_filename("o1-overhead-evidence", digest),
                evidence.model_dump(mode="json", exclude_none=False),
            )

    if o2_probe is not None:
        for digest, evidence in o2_probe.evidence_by_digest.items():
            _atomic_write_json(
                control_dir / _evidence_filename("o2-probe-evidence", digest),
                evidence.model_dump(mode="json", exclude_none=False),
            )
        for digest, evidence in o2_probe.overhead_evidence_by_digest.items():
            _atomic_write_json(
                control_dir / _evidence_filename("o2-overhead-evidence", digest),
                evidence.model_dump(mode="json", exclude_none=False),
            )

    index = DynamicCollectionEvidenceIndex(
        o1_runs_by_window=o1_runs_by_window,
        o1_overhead_digests_by_window=o1_overhead_by_window,
        o2_probe_evidence_digests=o2_probe_digests,
        o2_overhead_evidence_digests=o2_overhead_digests,
    )
    _atomic_write_json(
        control_dir / "dynamic-collection-evidence-index.json",
        index.model_dump(mode="json", exclude_none=False),
    )
    return index


__all__ = [
    "DYNAMIC_COLLECTION_EVIDENCE_INDEX_SCHEMA",
    "O2_COMPONENT_PROBE_EVIDENCE_SCHEMA",
    "DynamicCollectionEvidenceIndex",
    "DynamicCollectionUnavailable",
    "O1LiveSource",
    "O2ComponentProbe",
    "O2ComponentProbeEvidence",
    "o1_live_source",
    "o2_component_probe",
    "persist_dynamic_collection_evidence",
]
