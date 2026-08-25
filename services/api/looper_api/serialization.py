from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from statistics import median
from typing import Any

from looper_core.contracts import Direction, ExperimentSpec
from looper_core.state import AttemptStatus, CandidateStatus, ExperimentStatus
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from looper_api.analysis_service import build_analysis_snapshot
from looper_api.benchmark_compatibility import single_node_contract
from looper_api.benchmark_defaults import benchmark_selection_defaults
from looper_api.benchmark_registration import selection_scenario_document
from looper_api.benchmark_runtime import (
    deployment_capabilities,
    provisioned_capabilities,
    provisioning_contract,
)
from looper_api.models import (
    ArtifactLinkRecord,
    AttemptRecord,
    BenchmarkRecord,
    BenchmarkRegistrationRecord,
    CandidateRecord,
    CheckRecord,
    EvaluationRecord,
    ExperimentRecord,
    ObservationRecord,
    TargetRecord,
)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


_METRIC_DECLARATION_FIELDS = (
    "unit",
    "direction",
    "kind",
    "required",
    "minimumSamples",
    "description",
    "presentation",
)


def _metric_definition(declaration: dict[str, Any]) -> dict[str, Any]:
    """Project a metric declaration to its API shape without inventing fields.

    Spec-level metrics declare ``unit``/``direction``/``kind`` while a
    workload-level override may declare only ``presentation``. Pass through
    exactly what the author wrote so the consumer can distinguish an absent
    measurement field from a missing metric.
    """
    return {
        field: declaration[field] for field in _METRIC_DECLARATION_FIELDS if field in declaration
    }


def _metric_definitions(metrics: dict[str, Any]) -> dict[str, Any]:
    return {name: _metric_definition(decl) for name, decl in metrics.items()}


def _workload_views(workloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for item in workloads:
        view: dict[str, Any] = {"id": item["id"], "name": item["name"]}
        if "metrics" in item:
            view["metrics"] = {
                name: _metric_definition(decl) for name, decl in item["metrics"].items()
            }
        views.append(view)
    return views


def _result_sections(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose only the bounded, declarative result-navigation contract to clients."""

    spec = manifest.get("spec") or {}
    extensions = spec.get("x-extensions") or {}
    presentation = extensions.get("resultPresentation") or {}
    raw_sections = presentation.get("sections") or []
    sections: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_sections[:2]:
        if not isinstance(raw, dict):
            continue
        section_id = str(raw.get("id") or "").strip()
        label = str(raw.get("label") or "").strip()
        metrics = raw.get("metrics")
        if not section_id or section_id in seen or not label or not isinstance(metrics, list):
            continue
        declared_metrics = [
            str(metric)
            for metric in metrics
            if isinstance(metric, str) and metric in spec.get("metrics", {})
        ]
        if not declared_metrics:
            continue
        seen.add(section_id)
        sections.append(
            {
                "id": section_id,
                "label": label[:40],
                "description": str(raw.get("description") or "")[:300],
                "view": str(raw.get("view") or "")[:80] or None,
                "metrics": declared_metrics,
            }
        )
    return sections


def benchmark_view(
    record: BenchmarkRecord,
    registration: BenchmarkRegistrationRecord | None = None,
) -> dict[str, Any]:
    manifest = record.manifest_json
    metadata = manifest["metadata"]
    spec = manifest["spec"]
    scenario = selection_scenario_document(record, registration)
    adapter = spec.get("adapter") or {}
    extensions = spec.get("x-extensions", {})
    execution_status = extensions.get("executionStatus", "executable")
    runtime = spec["runtime"]
    package_ready = bool(
        record.manifest_path
        or (runtime.get("type") == "container" and "@sha256:" in str(runtime.get("image") or ""))
    )
    runnable = bool(
        execution_status == "executable"
        and package_ready
        and (record.trusted or runtime.get("type") == "container")
    )
    selectable = bool(extensions.get("selectable", runnable)) and runnable
    execution_blocker = extensions.get("executionBlocker")
    execution_blocker_reason = extensions.get("executionBlockerReason")
    metadata_extensions = metadata.get("x-extensions") or {}
    explicit_category = metadata_extensions.get("category") or extensions.get("category")
    view = {
        "id": record.benchmark_id,
        "key": record.key,
        "name": record.name,
        "description": record.description,
        "category": explicit_category or ("scenario" if scenario else "unclassified"),
        # A scenario only appears in the experiment picker when it can really
        # be delivered to a target and executed. Stage-0 contracts remain in
        # the catalog for research, but are never presented as runnable choices.
        "selectionReady": scenario is not None and selectable,
        "singleNodeReady": bool(
            scenario is not None and selectable and single_node_contract(manifest) is not None
        ),
        "selectable": selectable,
        "executionModel": adapter.get("executionModel", "custom"),
        "inputs": adapter.get("inputs", []),
        "infrastructure": spec.get("infrastructure"),
        "auditPolicy": spec.get("audit"),
        "selectionDefaults": benchmark_selection_defaults(manifest),
        "executionPolicy": spec.get("runtime", {}).get("executionPolicy"),
        "version": record.version,
        "license": record.license,
        "manifestDigest": record.manifest_digest,
        "metrics": list(spec["metrics"]),
        "metricDefinitions": _metric_definitions(spec.get("metrics", {})),
        "resultSections": _result_sections(manifest),
        "workloads": _workload_views(spec.get("workloads", [])),
        "cases": len(spec["workloads"]),
        "updatedAt": _iso(record.installed_at),
        "tags": manifest["spec"].get("capabilities", []),
        "deploymentRequirements": sorted(deployment_capabilities(manifest)),
        "provisionedCapabilities": sorted(provisioned_capabilities(manifest)),
        "provisioning": provisioning_contract(manifest),
        "packageReady": package_ready,
        "packageDigest": record.package_digest,
        "trusted": record.trusted,
        "executionStatus": execution_status,
        "runnable": runnable,
        "executionBlocker": execution_blocker,
        "executionBlockerReason": execution_blocker_reason,
        "registrationId": registration.id if registration else None,
        "registrationStatus": registration.status if registration else None,
        "scenario": scenario,
        "decisionQuestion": scenario.get("decision_question") if scenario else None,
        "primaryMetric": (
            scenario.get("primary_metric") if scenario else adapter.get("primaryMetric")
        ),
    }
    if registration is not None:
        view["auditStatus"] = "registered-not-admitted"
    return view


def target_view(record: TargetRecord) -> dict[str, Any]:
    status_map = {
        "available": "online",
        "inventory-only": "unknown",
        "degraded": "degraded",
        "offline": "offline",
    }
    inventory = record.inventory_json
    provider_state = str(inventory.get("instance_state") or inventory.get("status") or "").upper()
    status = status_map.get(record.status, "unknown")
    if record.lifecycle_status in {"missing", "archived"}:
        status = "offline"
    if (
        record.status == "inventory-only"
        and inventory.get("source") == "ssh-discovery"
        and record.lifecycle_status == "active"
    ):
        # SSH discovery is a persisted, verified inventory observation. A
        # missing Worker means execution is not ready; it does not make the
        # already-probed machine an unknown resource.
        status = "inventory"
    elif record.status == "inventory-only" and provider_state == "RUNNING":
        status = "inventory" if record.lifecycle_status == "active" else "offline"
    elif record.status == "inventory-only" and provider_state in {"STOPPED", "TERMINATED"}:
        status = "offline"
    fingerprint = record.fingerprint_json or {}
    inventory = record.inventory_json or {}
    display_snapshot = fingerprint.get("cloud_display")
    display_fingerprint = fingerprint
    display_inventory = inventory
    if isinstance(display_snapshot, dict):
        snapshot_fingerprint = display_snapshot.get("fingerprint")
        snapshot_inventory = display_snapshot.get("inventory")
        if isinstance(snapshot_fingerprint, dict):
            display_fingerprint = snapshot_fingerprint
        if isinstance(snapshot_inventory, dict):
            display_inventory = snapshot_inventory

    # Cloud inventory records created by older syncs kept hardware fields in
    # inventory_json, while external targets keep them in fingerprint_json.
    def first_value(*keys: str) -> Any:
        for source in (display_fingerprint, display_inventory):
            for key in keys:
                value = source.get(key)
                if value is not None and value != "":
                    return value
        return None

    # Architecture-like strings that are not real processor names
    _ARCH_LIKE = {
        "x86_64",
        "amd64",
        "AMD64",
        "aarch64",
        "arm64",
        "armv7l",
        "armv8l",
        "i386",
        "i686",
    }
    processor = first_value("processor")
    if processor and str(processor).strip() in _ARCH_LIKE:
        # Some Linux probes report the architecture in processor while the
        # detailed model is available under cpu.model_name.
        processor = None
        for source in (display_fingerprint, display_inventory):
            cpu_details = source.get("cpu")
            if isinstance(cpu_details, dict):
                processor = cpu_details.get("model_name") or cpu_details.get("model")
                if processor:
                    break

    # CPU: real processor name, or cloud instance type
    cpu = processor or first_value("instance_type") or ""

    image_id = first_value("image_id")

    # Architecture: preserve provider metadata, then explicit image metadata.
    arch = first_value("architecture", "machine", "platform", "cpu_architecture")
    if not arch:
        image_marker = str(image_id or "").casefold()
        if any(marker in image_marker for marker in ("aarch64", "arm64", "arm_", "_arm")):
            arch = "aarch64"
        elif any(marker in image_marker for marker in ("x86_64", "x64", "_amd64", "amd64")):
            arch = "x86_64"
    arch = arch or ""

    # Cores: external targets use logical_cpu_count; cloud inventory uses cpu.
    cores = first_value("logical_cpu_count", "cpu", "vcpu", "vcpus")

    # Memory: prefer memory_gib, fallback to memory_bytes.
    memory_gib = first_value("memory_gib")
    if memory_gib is None:
        memory_bytes = first_value("memory_bytes")
        if memory_bytes:
            memory_gib = round(memory_bytes / (1024**3), 1)

    hardware_parts = [
        cpu if cpu else None,
        arch if arch else None,
        f"{cores} vCPU" if cores else None,
        f"{memory_gib:g} GiB" if memory_gib else None,
    ]
    return {
        "id": record.id,
        "name": record.name,
        "type": record.provider,
        "provider": record.provider,
        "orderId": inventory.get("order_id"),
        "endpoint": (
            inventory.get("endpoint")
            or inventory.get("public_ip")
            or inventory.get("private_ip")
            or ("local" if record.id == "local" else "—")
        ),
        "status": status,
        # Cloud resources consistently show the purchased image. SSH probes
        # enrich the fingerprint with the running OS, but must not replace the
        # stable image identity in the resource table.
        "framework": (
            f"镜像 {image_id}"
            if image_id
            else display_inventory.get("framework") or display_fingerprint.get("system")
        ),
        "version": None if image_id else display_fingerprint.get("release"),
        "hardware": " · ".join(str(item) for item in hardware_parts if item),
        "lastSeenAt": _iso(record.last_inventory_seen_at or record.updated_at),
        "tags": record.capabilities_json,
        "runnable": record.runnable and record.lifecycle_status == "active",
        "lifecycleStatus": record.lifecycle_status,
        "lastInventorySeenAt": _iso(record.last_inventory_seen_at),
        "missingSince": _iso(record.inventory_missing_since),
        "inventoryMissCount": record.inventory_miss_count,
        "archivedAt": _iso(record.archived_at),
        "archiveReason": record.archive_reason,
        "snapshotDigest": record.snapshot_digest,
        "fingerprint": fingerprint,
        "sshAutomation": inventory.get("autoSsh") or {"status": "manual"},
    }


def _analysis_by_candidate(
    session: Session, experiment: ExperimentRecord
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    candidates_exist = session.scalar(
        select(CandidateRecord.id).where(CandidateRecord.experiment_id == experiment.id).limit(1)
    )
    if not candidates_exist:
        return {}, None
    analysis = build_analysis_snapshot(session, experiment.id, persist=False)
    return {item["id"]: item for item in analysis.get("candidates", [])}, analysis


def _artifact_views(session: Session, attempt_ids: list[str]) -> list[dict[str, Any]]:
    if not attempt_ids:
        return []
    links = list(
        session.scalars(
            select(ArtifactLinkRecord).where(ArtifactLinkRecord.attempt_id.in_(attempt_ids))
        )
    )
    return [
        {
            "name": item.name,
            "url": f"/api/v1/artifacts/{item.digest}",
            "type": item.media_type,
            "digest": item.digest,
            "role": item.role,
            "attemptId": item.attempt_id,
        }
        for item in links
    ]


def _best_primary_score(
    results: list[dict[str, Any]], metric: str, direction: Direction
) -> float | None:
    scores: list[float] = []
    for result in results:
        objective = next(
            (item for item in result.get("objectives", []) if item.get("metric") == metric),
            None,
        )
        raw = objective.get("raw") if objective else None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            scores.append(float(raw))
    if not scores:
        return None
    return max(scores) if direction == Direction.MAXIMIZE else min(scores)


def _observation_metric_view(
    item: ObservationRecord, declaration: dict[str, Any]
) -> dict[str, Any] | None:
    value: float | bool | None = (
        item.value_boolean if item.value_boolean is not None else item.value_number
    )
    if value is None:
        return None
    direction = declaration.get("direction")
    return {
        "name": item.metric,
        "value": value,
        "unit": item.unit,
        "sampleIndex": item.sample_index,
        "sampleCount": item.sample_count,
        "statistic": item.statistic,
        "baseline": None,
        "direction": "max"
        if direction == "maximize"
        else "min"
        if direction == "minimize"
        else "none",
    }


def _mean_observation_metric_views(
    observations: list[ObservationRecord], declarations: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return one display value per metric across successful repeat collections.

    Raw observations remain immutable evidence. The experiment detail response is
    intentionally a compact presentation projection: numeric observations use an
    arithmetic mean and boolean gates pass only when every contributing collection
    passed. ``sampleCount`` tells the UI how many raw observations contributed.
    """

    grouped: dict[str, list[ObservationRecord]] = defaultdict(list)
    order: list[str] = []
    for observation in observations:
        if observation.phase == "warmup":
            continue
        if observation.metric not in grouped:
            order.append(observation.metric)
        grouped[observation.metric].append(observation)

    views: list[dict[str, Any]] = []
    for metric in order:
        items = grouped[metric]
        numeric_values = [
            float(item.value_number) for item in items if item.value_number is not None
        ]
        boolean_values = [
            bool(item.value_boolean) for item in items if item.value_boolean is not None
        ]
        if numeric_values:
            value: float | bool = sum(numeric_values) / len(numeric_values)
            sample_count = len(numeric_values)
            statistic = "mean"
        elif boolean_values:
            value = all(boolean_values)
            sample_count = len(boolean_values)
            statistic = "all"
        else:
            continue
        declaration = declarations.get(metric, {})
        direction = declaration.get("direction")
        views.append(
            {
                "name": metric,
                "value": value,
                "unit": items[0].unit,
                "sampleIndex": None,
                "sampleCount": sample_count,
                "statistic": statistic,
                "baseline": None,
                "direction": "max"
                if direction == "maximize"
                else "min"
                if direction == "minimize"
                else "none",
            }
        )
    return views


def _attempt_metric_views(
    observations: list[ObservationRecord], metric_specs: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return the bounded metric view for one attempt.

    Streaming observations can contain many samples for the same metric. The
    detail page needs the declared sample metrics, while scalar metrics use
    the newest observation just as the historical view did.
    """
    latest_by_metric = {item.metric: item for item in observations}
    display = [
        item
        for item in observations
        if metric_specs.get(item.metric, {}).get("kind") == "sample"
        or latest_by_metric[item.metric] is item
    ]
    return [
        view
        for item in display
        if (view := _observation_metric_view(item, metric_specs.get(item.metric, {}))) is not None
    ]


def _average_metric_views(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Average numeric scalar metrics across all measured rounds."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        for metric in run.get("metrics", []):
            grouped.setdefault(metric["name"], []).append(metric)
    result: list[dict[str, Any]] = []
    for _name, values in grouped.items():
        if any(
            item.get("sampleIndex") is not None or item.get("statistic") == "sample"
            for item in values
        ):
            # Raw sample series are evidence, not scalar round summaries. Keep
            # every sample instead of collapsing them into another mean.
            result.extend(values)
            continue
        numeric = [
            item["value"]
            for item in values
            if isinstance(item.get("value"), (int, float))
            and not isinstance(item.get("value"), bool)
        ]
        if numeric:
            sample = values[-1].copy()
            sample["value"] = sum(float(item) for item in numeric) / len(numeric)
            sample["sampleCount"] = len(numeric)
            sample["statistic"] = "mean"
            result.append(sample)
        else:
            result.append(values[-1])
    return result


def _workload_primary_metric(
    manifest: dict[str, Any] | None, workload_id: str | None
) -> str | None:
    """Return the workload-specific primary outcome declared by the package."""

    if not manifest or not workload_id:
        return None
    workloads = manifest.get("spec", {}).get("workloads", [])
    workload = next(
        (item for item in workloads if str(item.get("id")) == workload_id),
        None,
    )
    if not workload:
        return None
    for name, declaration in (workload.get("metrics") or {}).items():
        roles = declaration.get("presentation", {}).get("roles", [])
        if "primary_outcome" in roles:
            return str(name)
    global_primary_metrics = [
        str(name)
        for name, declaration in (manifest.get("spec", {}).get("metrics") or {}).items()
        if "primary_outcome" in declaration.get("presentation", {}).get("roles", [])
    ]
    if len(global_primary_metrics) == 1:
        return global_primary_metrics[0]
    return None


def _comparison_axes(manifest: dict[str, Any]) -> list[dict[str, str]]:
    spec = manifest.get("spec") or {}
    declarations = spec.get("metrics") or {}
    workloads = spec.get("workloads") or []
    benchmark_id = str((manifest.get("metadata") or {}).get("id") or "")
    metric_profile = {
        "dcperf.mediawiki.closed-loop": [
            ("closed_loop_successful_rps", "成功请求率"),
            ("latency_p50_ms", "P50 延迟"),
            ("latency_p95_ms", "P95 延迟"),
            ("latency_p99_ms", "P99 延迟"),
        ]
    }.get(benchmark_id)
    axes: list[dict[str, str]] = []
    if metric_profile:
        for workload in workloads:
            workload_id = str(workload.get("id") or "")
            for metric, label in metric_profile:
                declaration = declarations.get(metric) or {}
                direction = str(declaration.get("direction") or "")
                unit = str(declaration.get("unit") or "")
                if not workload_id or direction not in {"maximize", "minimize"} or not unit:
                    continue
                axes.append(
                    {
                        "key": metric,
                        "workloadId": workload_id,
                        "label": label,
                        "metric": metric,
                        "unit": unit,
                        "direction": direction,
                    }
                )
        return axes

    for workload in workloads:
        workload_id = str(workload.get("id") or "")
        metric = _workload_primary_metric(manifest, workload_id)
        declaration = declarations.get(metric or "") or {}
        direction = str(declaration.get("direction") or "")
        unit = str(declaration.get("unit") or "")
        if not workload_id or not metric or direction not in {"maximize", "minimize"} or not unit:
            continue
        axes.append(
            {
                "key": workload_id,
                "workloadId": workload_id,
                "label": str(workload.get("name") or workload_id),
                "metric": metric,
                "unit": unit,
                "direction": direction,
            }
        )
    return axes


def _normalize_comparison_values(values: dict[str, float], direction: str) -> dict[str, float]:
    finite = {
        key: float(value) for key, value in values.items() if math.isfinite(value) and value >= 0
    }
    if len(finite) < 2:
        return {}
    if direction == "maximize":
        best = max(finite.values())
        if best == 0:
            return {key: 100.0 for key in finite}
        return {key: min(100.0, max(0.0, value / best * 100)) for key, value in finite.items()}
    positive = {key: value for key, value in finite.items() if value > 0}
    if len(positive) < 2:
        return {}
    best = min(positive.values())
    return {key: min(100.0, best / value * 100) for key, value in positive.items()}


def _scenario_comparison_views(
    session: Session, experiments: list[ExperimentRecord]
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    experiment_meta: dict[str, dict[str, Any]] = {}
    for record in experiments:
        if record.status != ExperimentStatus.COMPLETED:
            continue
        spec = ExperimentSpec.model_validate(record.spec_json)
        if spec.mode.value != "selection" or spec.scenario is None:
            continue
        benchmark = session.scalar(
            select(BenchmarkRecord).where(
                BenchmarkRecord.benchmark_id == spec.benchmark_id,
                BenchmarkRecord.version == spec.benchmark_version,
            )
        )
        if benchmark is None:
            continue
        axes = _comparison_axes(benchmark.manifest_json)
        if not axes:
            continue
        key = (spec.scenario.id, spec.benchmark_id, spec.benchmark_version)
        group = groups.setdefault(
            key,
            {
                "id": "@".join(key),
                "scenarioId": spec.scenario.id,
                "scenarioName": spec.scenario.name,
                "benchmarkId": spec.benchmark_id,
                "benchmarkName": benchmark.name,
                "benchmarkVersion": spec.benchmark_version,
                "axes": axes,
                "updatedAt": None,
                "targets": {},
            },
        )
        binding_labels = {
            item.target_id: item.label
            for item in (spec.selection.target_bindings if spec.selection else [])
        }
        experiment_meta[record.id] = {
            "record": record,
            "group": group,
            "axesByWorkload": {
                workload_id: [axis for axis in group["axes"] if axis["workloadId"] == workload_id]
                for workload_id in {axis["workloadId"] for axis in group["axes"]}
            },
            "requiredChecks": set(
                (benchmark.manifest_json.get("spec") or {})
                .get("adapter", {})
                .get("requiredChecks", [])
            ),
            "bindingLabels": binding_labels,
        }

    experiment_ids = list(experiment_meta)
    evaluations = (
        list(
            session.scalars(
                select(EvaluationRecord).where(EvaluationRecord.experiment_id.in_(experiment_ids))
            )
        )
        if experiment_ids
        else []
    )
    eligible_evaluations = {
        item.id: item
        for item in evaluations
        if CandidateStatus(item.status) == CandidateStatus.FEASIBLE
    }
    attempts = (
        list(
            session.scalars(
                select(AttemptRecord).where(
                    AttemptRecord.evaluation_id.in_(list(eligible_evaluations)),
                    AttemptRecord.status == AttemptStatus.SUCCEEDED,
                )
            )
        )
        if eligible_evaluations
        else []
    )
    attempt_ids = [item.id for item in attempts]
    checks_by_attempt: dict[str, dict[str, bool]] = defaultdict(dict)
    observations_by_attempt: dict[str, list[ObservationRecord]] = defaultdict(list)
    if attempt_ids:
        for check in session.scalars(
            select(CheckRecord).where(CheckRecord.attempt_id.in_(attempt_ids))
        ):
            checks_by_attempt[check.attempt_id][check.check_id] = check.passed
        for observation in session.scalars(
            select(ObservationRecord).where(ObservationRecord.attempt_id.in_(attempt_ids))
        ):
            observations_by_attempt[observation.attempt_id].append(observation)

    attempts_by_evaluation: dict[str, list[AttemptRecord]] = defaultdict(list)
    for attempt in attempts:
        meta = experiment_meta[attempt.experiment_id]
        required_checks = meta["requiredChecks"]
        check_results = checks_by_attempt[attempt.id]
        if all(check_results.get(check_id) is True for check_id in required_checks):
            attempts_by_evaluation[attempt.evaluation_id].append(attempt)

    for evaluation in eligible_evaluations.values():
        meta = experiment_meta[evaluation.experiment_id]
        axes = meta["axesByWorkload"].get(evaluation.workload_id, [])
        if not axes:
            continue
        axis_results: list[tuple[dict[str, str], float, int]] = []
        for axis in axes:
            attempt_values: list[float] = []
            for attempt in attempts_by_evaluation[evaluation.id]:
                values = [
                    float(item.value_number)
                    for item in observations_by_attempt[attempt.id]
                    if item.metric == axis["metric"]
                    and item.phase != "warmup"
                    and item.value_number is not None
                    and math.isfinite(float(item.value_number))
                ]
                if values:
                    attempt_values.append(sum(values) / len(values))
            if not attempt_values:
                continue
            valid_sample_count = sum(
                1
                for attempt in attempts_by_evaluation[evaluation.id]
                for item in observations_by_attempt[attempt.id]
                if item.metric == axis["metric"]
                and item.phase != "warmup"
                and item.value_number is not None
                and math.isfinite(float(item.value_number))
            )
            axis_results.append(
                (axis, sum(attempt_values) / len(attempt_values), valid_sample_count)
            )
        if not axis_results:
            continue
        record = meta["record"]
        group = meta["group"]
        target_record = session.get(TargetRecord, evaluation.target_id)
        label = (
            meta["bindingLabels"].get(evaluation.target_id)
            or (target_record.name if target_record else None)
            or evaluation.target_id
        )
        target = group["targets"].setdefault(
            evaluation.target_id,
            {
                "targetId": evaluation.target_id,
                "label": label,
                "updatedAt": None,
                "studies": set(),
                "values": defaultdict(lambda: defaultdict(list)),
                "sampleCounts": defaultdict(lambda: defaultdict(int)),
            },
        )
        updated_at = _iso(record.updated_at)
        target["label"] = label
        target["updatedAt"] = max(filter(None, [target["updatedAt"], updated_at]), default=None)
        target["studies"].add(record.id)
        for axis, value, valid_sample_count in axis_results:
            target["values"][axis["key"]][record.id].append(value)
            target["sampleCounts"][axis["key"]][record.id] += valid_sample_count
        group["updatedAt"] = max(filter(None, [group["updatedAt"], updated_at]), default=None)

    comparisons: list[dict[str, Any]] = []
    for group in groups.values():
        raw_targets = group.pop("targets")
        normalized_by_axis: dict[str, dict[str, float]] = {}
        kept_axes: list[dict[str, str]] = []
        medians_by_target: dict[str, dict[str, float]] = defaultdict(dict)
        for axis in group["axes"]:
            raw_values: dict[str, float] = {}
            for target_id, target in raw_targets.items():
                studies = target["values"].get(axis["key"], {})
                study_values = [sum(values) / len(values) for values in studies.values() if values]
                if study_values:
                    raw_values[target_id] = float(median(study_values))
            normalized = _normalize_comparison_values(raw_values, axis["direction"])
            if len(normalized) < 2:
                continue
            kept_axes.append(axis)
            normalized_by_axis[axis["key"]] = normalized
            for target_id, value in raw_values.items():
                if target_id in normalized:
                    medians_by_target[target_id][axis["key"]] = value

        targets: list[dict[str, Any]] = []
        for target_id, target in raw_targets.items():
            values = {}
            for axis in kept_axes:
                if axis["key"] not in medians_by_target[target_id]:
                    continue
                studies = target["values"][axis["key"]]
                values[axis["key"]] = {
                    "raw": medians_by_target[target_id][axis["key"]],
                    "normalized": normalized_by_axis[axis["key"]][target_id],
                    "studyCount": len(studies),
                    "sampleCount": sum(target["sampleCounts"][axis["key"]].values()),
                }
            if values:
                targets.append(
                    {
                        "targetId": target_id,
                        "label": target["label"],
                        "updatedAt": target["updatedAt"],
                        "studyCount": len(target["studies"]),
                        "validSampleCount": sum(value["sampleCount"] for value in values.values()),
                        "values": values,
                    }
                )
        targets.sort(
            key=lambda item: (
                len(item["values"]),
                item.get("updatedAt") or "",
                item["label"],
            ),
            reverse=True,
        )
        if len(targets) < 2 or not kept_axes:
            continue
        group["axes"] = kept_axes
        group["targets"] = targets
        comparisons.append(group)
    comparisons.sort(key=lambda item: item.get("updatedAt") or "", reverse=True)
    return comparisons


def experiment_view(
    session: Session, record: ExperimentRecord, *, detail: bool = False
) -> dict[str, Any]:
    spec = ExperimentSpec.model_validate(record.spec_json)
    candidates = list(
        session.scalars(
            select(CandidateRecord)
            .where(CandidateRecord.experiment_id == record.id)
            .order_by(CandidateRecord.sequence)
        )
    )
    attempts = list(
        session.scalars(select(AttemptRecord).where(AttemptRecord.experiment_id == record.id))
    )
    terminal_attempts = sum(
        1
        for attempt in attempts
        if AttemptStatus(attempt.status)
        in {
            AttemptStatus.SUCCEEDED,
            AttemptStatus.FAILED,
            AttemptStatus.TIMED_OUT,
            AttemptStatus.CANCELLED,
            AttemptStatus.LOST,
        }
    )
    budget_terminal_attempts = sum(
        1
        for attempt in attempts
        if attempt.retry_index == 0
        and AttemptStatus(attempt.status)
        in {
            AttemptStatus.SUCCEEDED,
            AttemptStatus.FAILED,
            AttemptStatus.TIMED_OUT,
            AttemptStatus.CANCELLED,
            AttemptStatus.LOST,
        }
    )
    terminal_candidates = sum(
        1
        for candidate in candidates
        if CandidateStatus(candidate.status)
        in {
            CandidateStatus.FEASIBLE,
            CandidateStatus.INFEASIBLE,
            CandidateStatus.INCONCLUSIVE,
            CandidateStatus.FAILED,
        }
    )
    active_attempt = max(
        (
            attempt
            for attempt in attempts
            if AttemptStatus(attempt.status)
            in {AttemptStatus.LEASED, AttemptStatus.RUNNING, AttemptStatus.UPLOADING}
        ),
        key=lambda item: item.leased_at or item.created_at,
        default=None,
    )
    analysis_map, analysis = _analysis_by_candidate(session, record)
    is_selection = spec.mode.value == "selection"
    selection_targets = {item["target_id"]: item for item in (analysis or {}).get("targets", [])}
    baseline = next((item for item in candidates if item.role == "baseline"), None)
    baseline_result = analysis_map.get(baseline.id) if baseline else None
    feasible = [item for item in analysis_map.values() if item.get("feasible")]
    primary_objective = spec.objectives[0]
    best_score = (
        None
        if is_selection
        else _best_primary_score(
            feasible,
            primary_objective.metric,
            primary_objective.direction,
        )
    )

    benchmark = session.scalar(
        select(BenchmarkRecord).where(
            BenchmarkRecord.benchmark_id == spec.benchmark_id,
            BenchmarkRecord.version == spec.benchmark_version,
        )
    )
    target = session.get(TargetRecord, spec.target_ids[0]) if spec.target_ids else None
    response: dict[str, Any] = {
        "id": record.id,
        "name": record.name,
        "description": record.description,
        "status": record.status,
        "mode": spec.mode,
        "targetId": target.id if target else None,
        "targetName": target.name if target else None,
        "targetIds": spec.target_ids,
        "targetNames": [
            item.name
            for target_id in spec.target_ids
            if (item := session.get(TargetRecord, target_id)) is not None
        ],
        "benchmarkId": spec.benchmark_id,
        "benchmarkName": benchmark.name if benchmark else spec.benchmark_id,
        "metricDefinitions": _metric_definitions(
            (benchmark.manifest_json.get("spec") or {}).get("metrics", {})
        )
        if benchmark
        else {},
        "resultSections": _result_sections(benchmark.manifest_json) if benchmark else [],
        "progress": round(
            100
            * (
                budget_terminal_attempts / spec.budget.max_attempts
                if is_selection
                else terminal_candidates / spec.budget.max_candidates
            ),
            1,
        )
        if (spec.budget.max_attempts if is_selection else spec.budget.max_candidates)
        else 0,
        "bestScore": best_score,
        "baselineScore": baseline_result["objectives"][0].get("raw")
        if baseline_result and baseline_result.get("objectives")
        else None,
        "createdAt": _iso(record.created_at),
        "updatedAt": _iso(record.updated_at),
        "attempts": budget_terminal_attempts,
        "actualAttempts": terminal_attempts,
        "maxAttempts": spec.budget.max_attempts,
        "objective": spec.objectives[0].metric,
        "decisionQuestion": spec.scenario.decision_question if spec.scenario else None,
        "scenario": spec.scenario.model_dump(mode="json") if spec.scenario else None,
        "comparison": (analysis or {}).get("comparisons", [None])[0]
        if (analysis or {}).get("comparisons")
        else None,
        "config": record.spec_json,
        "candidateCount": len(candidates),
        "revision": record.revision,
        "analysisStatus": analysis.get("status") if analysis else None,
        "activePhase": active_attempt.phase if active_attempt else None,
        "activePhaseDetail": active_attempt.phase_detail if active_attempt else None,
    }
    if not detail:
        return response

    evaluations = list(
        session.scalars(
            select(EvaluationRecord)
            .where(EvaluationRecord.experiment_id == record.id)
            .order_by(EvaluationRecord.created_at.desc())
        )
    )
    evaluation_views: list[dict[str, Any]] = []
    all_attempt_ids = [item.id for item in attempts]
    artifacts = _artifact_views(session, all_attempt_ids)
    artifacts_by_attempt: dict[str, list[dict[str, Any]]] = {}
    for item in artifacts:
        artifacts_by_attempt.setdefault(item["attemptId"], []).append(item)
    for evaluation in evaluations:
        candidate = session.get(CandidateRecord, evaluation.candidate_id)
        evaluation_attempts = [item for item in attempts if item.evaluation_id == evaluation.id]
        latest = max(
            evaluation_attempts,
            key=lambda item: (item.repeat_index, item.retry_index, item.created_at),
            default=None,
        )
        # A selection evaluation owns all repeat Attempts. The highest repeat
        # can still be queued while an earlier repeat already contains a valid
        # measurement. Use the newest Attempt with observations for result
        # display, while retaining ``latest`` for the live evaluation status.
        attempts_with_observations = set(
            session.scalars(
                select(ObservationRecord.attempt_id).where(
                    ObservationRecord.attempt_id.in_([item.id for item in evaluation_attempts])
                )
            )
        )
        latest_result = max(
            (item for item in evaluation_attempts if item.id in attempts_with_observations),
            key=lambda item: (
                item.completed_at or item.started_at or item.created_at,
                item.repeat_index,
                item.retry_index,
            ),
            default=None,
        )
        candidate_analysis = analysis_map.get(candidate.id, {}) if candidate else {}
        target_analysis = selection_targets.get(evaluation.target_id, {}) if is_selection else {}
        objective_rows = (
            target_analysis.get("metrics", [])
            if is_selection
            else candidate_analysis.get("objectives", [])
        )
        status_map = {
            CandidateStatus.PENDING: "queued",
            CandidateStatus.RUNNING: "running",
            CandidateStatus.FEASIBLE: "completed",
            CandidateStatus.INFEASIBLE: "failed",
            CandidateStatus.INCONCLUSIVE: "failed",
            CandidateStatus.FAILED: "failed",
        }
        metrics = [
            {
                "name": item["metric"],
                "value": item["raw"],
                "unit": item["unit"],
                "baseline": None,
                "direction": "max" if item["direction"] == "maximize" else "min",
            }
            for item in objective_rows
            if item.get("raw") is not None
        ]
        # Preserve every attempt for the per-round view, including failed
        # retries that still uploaded partial evidence.
        metric_specs = (
            benchmark.manifest_json.get("spec", {}).get("metrics", {}) if benchmark else {}
        )
        required_checks = set(
            benchmark.manifest_json.get("spec", {}).get("adapter", {}).get("requiredChecks", [])
            if benchmark
            else []
        )
        run_views: list[dict[str, Any]] = []
        for attempt in sorted(
            evaluation_attempts,
            key=lambda item: (item.repeat_index, item.retry_index, item.created_at),
        ):
            observations = list(
                session.scalars(
                    select(ObservationRecord)
                    .where(ObservationRecord.attempt_id == attempt.id)
                    .order_by(ObservationRecord.created_at)
                )
            )
            checks = list(
                session.scalars(
                    select(CheckRecord)
                    .where(CheckRecord.attempt_id == attempt.id)
                    .order_by(CheckRecord.check_id)
                )
            )
            run_metrics = _attempt_metric_views(observations, metric_specs)
            check_results = {check.check_id: check.passed for check in checks}
            required_checks_passed = all(
                check_results.get(check_id) is True for check_id in required_checks
            )
            attempt_status = AttemptStatus(attempt.status)
            valid_measurement = attempt_status == AttemptStatus.SUCCEEDED and required_checks_passed
            terminal_failure = attempt_status in {
                AttemptStatus.FAILED,
                AttemptStatus.TIMED_OUT,
                AttemptStatus.CANCELLED,
                AttemptStatus.LOST,
            }
            run_views.append(
                {
                    "attemptId": attempt.id,
                    "round": attempt.repeat_index + 1,
                    "retry": attempt.retry_index,
                    "status": "completed"
                    if valid_measurement
                    else "failed"
                    if terminal_failure or attempt_status == AttemptStatus.SUCCEEDED
                    else attempt_status.value,
                    "measured": bool(run_metrics),
                    "startedAt": _iso(attempt.started_at),
                    "completedAt": _iso(attempt.completed_at),
                    "metrics": run_metrics,
                    "gateResults": [
                        {
                            "id": check.check_id,
                            "passed": check.passed,
                            "kind": check.kind,
                            "message": check.message,
                            "details": check.details_json,
                        }
                        for check in checks
                    ],
                    "artifacts": artifacts_by_attempt.get(attempt.id, []),
                    "error": attempt.error_message,
                }
            )
        valid_runs = [
            item for item in run_views if item["status"] == "completed" and item["metrics"]
        ]
        if valid_runs:
            metrics = _average_metric_views(valid_runs)
        elif run_views and is_selection:
            # Target-level analysis can contain a valid result from another
            # workload. It must not make this failed evaluation look measured.
            metrics = []
        workload_primary = _workload_primary_metric(
            benchmark.manifest_json if benchmark else None,
            evaluation.workload_id,
        )
        workload_score = next(
            (
                item.get("value")
                for item in metrics
                if item.get("name") == workload_primary
                and isinstance(item.get("value"), (int, float))
                and not isinstance(item.get("value"), bool)
            ),
            None,
        )
        failed_check_ids: list[str] = []
        if latest and AttemptStatus(latest.status) == AttemptStatus.FAILED:
            failed_check_ids = list(
                session.scalars(
                    select(CheckRecord.check_id)
                    .where(CheckRecord.attempt_id == latest.id, CheckRecord.passed.is_(False))
                    .order_by(CheckRecord.check_id)
                )
            )
        phase_detail = latest.phase_detail if latest else None
        if failed_check_ids:
            phase_detail = f"采集数据已回传；校验未通过：{', '.join(failed_check_ids)}"
        elif (
            latest and AttemptStatus(latest.status) == AttemptStatus.FAILED and latest.error_message
        ):
            phase_detail = f"采集流程失败：{latest.error_message}"
        duration = None
        if latest and latest.started_at and latest.completed_at:
            duration = (latest.completed_at - latest.started_at).total_seconds()
        evaluation_views.append(
            {
                "id": evaluation.id,
                "attemptId": latest.id if latest else None,
                "resultAttemptId": latest_result.id if latest_result else None,
                "candidate": target_analysis.get("label")
                if is_selection
                else _candidate_label(candidate),
                "candidateId": candidate.id if candidate else None,
                "parameters": candidate.parameters_json if candidate else {},
                "status": status_map[CandidateStatus(evaluation.status)],
                "phase": latest.phase if latest else None,
                "phaseDetail": phase_detail,
                "score": workload_score
                if workload_primary
                else objective_rows[0].get("raw")
                if objective_rows
                else None,
                "duration": duration,
                "createdAt": _iso(evaluation.created_at),
                "metrics": metrics,
                "runs": run_views,
                "artifacts": [
                    artifact
                    for attempt in evaluation_attempts
                    for artifact in artifacts_by_attempt.get(attempt.id, [])
                ],
                "error": latest.error_message if latest else None,
                "workload": evaluation.workload_id,
                "targetId": evaluation.target_id,
                "paretoRank": candidate_analysis.get("pareto_rank") if not is_selection else None,
                "gateResults": target_analysis.get("gates", [])
                if is_selection
                else candidate_analysis.get("gates", []),
            }
        )
    response["evaluations"] = evaluation_views
    response["artifacts"] = artifacts
    response["analysis"] = analysis
    return response


def _candidate_label(candidate: CandidateRecord | None) -> str:
    if candidate is None:
        return "Unknown candidate"
    if candidate.role == "baseline":
        return "Baseline"
    if candidate.role == "scenario":
        return "Scenario workload"
    pairs = [f"{name}={value}" for name, value in candidate.parameters_json.items()]
    return " · ".join(pairs)


def analysis_view(result: dict[str, Any]) -> dict[str, Any]:
    candidates = {item["id"]: item for item in result.get("candidates", [])}
    pareto = []
    for point in result.get("pareto", []):
        candidate = candidates.get(point["candidate_id"], {})
        objectives = candidate.get("objectives", [])
        pareto.append(
            {
                "id": point["candidate_id"],
                "candidate": _analysis_candidate_label(candidate),
                "score": objectives[0].get("raw", 0) if objectives else 0,
                "cost": objectives[1].get("raw", 0) if len(objectives) > 1 else 0,
                "latency": next(
                    (item.get("raw") for item in objectives if "latency" in item["metric"]), None
                ),
                "rank": point.get("rank"),
                "feasible": point.get("feasible"),
                "objectives": point.get("objectives", {}),
            }
        )
    evidence_summary = result.get("evidence", {})
    evidence = [
        {
            "id": "evidence-summary",
            "title": "完整实验原始证据",
            "kind": "内容寻址",
            "summary": (
                f"{evidence_summary.get('attempt_count', 0)} 次尝试 · "
                f"{evidence_summary.get('observation_count', 0)} 条观测 · "
                f"{evidence_summary.get('artifact_count', 0)} 个制品"
            ),
            "createdAt": None,
            "artifacts": [
                {
                    "name": "完整证据包",
                    "url": f"/api/v1/experiments/{result['experiment_id']}/evidence",
                    "type": "application/zip",
                }
            ],
        }
    ]
    return {
        **result,
        "pareto": pareto,
        "evidenceSummary": evidence_summary,
        "evidence": evidence,
    }


def _analysis_candidate_label(candidate: dict[str, Any]) -> str:
    if candidate.get("role") == "baseline":
        return "Baseline"
    return " · ".join(f"{key}={value}" for key, value in candidate.get("parameters", {}).items())


def dashboard_view(session: Session) -> dict[str, Any]:
    experiments = list(
        session.scalars(select(ExperimentRecord).order_by(ExperimentRecord.created_at.desc()))
    )
    counts = Counter(record.status for record in experiments)
    active = [
        record
        for record in experiments
        if record.status
        in {ExperimentStatus.QUEUED, ExperimentStatus.RUNNING, ExperimentStatus.PAUSED}
    ]
    terminal = [
        record
        for record in experiments
        if record.status in {ExperimentStatus.COMPLETED, ExperimentStatus.FAILED}
    ]
    success_count = sum(1 for record in terminal if record.status == ExperimentStatus.COMPLETED)
    attempt_seconds = session.scalar(
        select(
            func.sum(
                func.julianday(AttemptRecord.completed_at)
                - func.julianday(AttemptRecord.started_at)
            )
            * 86400
        ).where(AttemptRecord.started_at.is_not(None), AttemptRecord.completed_at.is_not(None))
    )
    trend = []
    for record in reversed(experiments[:12]):
        view = experiment_view(session, record)
        if view.get("bestScore") is not None:
            trend.append(
                {
                    "time": view.get("updatedAt") or view.get("createdAt"),
                    "score": view["bestScore"],
                    "baseline": view.get("baselineScore"),
                }
            )
    return {
        "counts": dict(counts),
        "activeExperiments": [experiment_view(session, item) for item in active[:6]],
        "recentExperiments": [experiment_view(session, item) for item in experiments[:8]],
        "trend": trend,
        "scenarioComparisons": _scenario_comparison_views(session, experiments),
        "successRate": success_count / len(terminal) if terminal else None,
        "totalExperiments": len(experiments),
        "computeHours": float(attempt_seconds or 0) / 3600,
    }
