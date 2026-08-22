from __future__ import annotations

import re
from collections import defaultdict
from decimal import ROUND_HALF_EVEN, Decimal
from random import Random
from typing import Any

from looper_core.canonical import canonical_digest, new_id, utc_now
from looper_core.contracts import (
    Aggregation,
    BudgetSpec,
    Comparison,
    Direction,
    ExperimentalDesign,
    ExperimentCreate,
    ExperimentMode,
    ExperimentSpec,
    FrontierPointEvidence,
    GateKind,
    GateScope,
    GateSpec,
    ObjectiveSpec,
    Operator,
    OptimizerSpec,
    ScenarioBenchmarkSpec,
    SearchParameter,
)
from looper_core.optimizer import SearchSpaceExhausted, suggest_candidate
from looper_core.selection import analyze_slo_frontier, frontier_block_from_scenario_result
from looper_core.state import (
    AttemptStatus,
    CandidateStatus,
    ExperimentStatus,
    require_experiment_transition,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from looper_api.events import append_event
from looper_api.models import (
    AnalysisSnapshotRecord,
    AttemptRecord,
    BenchmarkRecord,
    CandidateRecord,
    CheckRecord,
    EvaluationRecord,
    ExperimentRecord,
    ObservationRecord,
    SelectionLoadPointRecord,
    TargetRecord,
)
from looper_api.seed import get_benchmark

TERMINAL_ATTEMPTS = {
    AttemptStatus.SUCCEEDED,
    AttemptStatus.FAILED,
    AttemptStatus.TIMED_OUT,
    AttemptStatus.CANCELLED,
    AttemptStatus.LOST,
}
TERMINAL_CANDIDATES = {
    CandidateStatus.FEASIBLE,
    CandidateStatus.INFEASIBLE,
    CandidateStatus.INCONCLUSIVE,
    CandidateStatus.FAILED,
}


class SchedulerError(ValueError):
    pass


def create_demo_request(name: str = "Compression Pareto study") -> ExperimentCreate:
    return ExperimentCreate(
        name=name,
        description="Local zlib search with correctness, throughput, latency, and ratio evidence.",
        spec=ExperimentSpec(
            benchmark_id="looper.demo.compression",
            benchmark_version="1.0.0",
            target_ids=["local"],
            workload_ids=["medium"],
            baseline_parameters={"compression_level": 6, "chunk_size": 16384},
            search_space={
                "compression_level": SearchParameter(
                    type="integer", minimum=1, maximum=9, step=1, default=6
                ),
                "chunk_size": SearchParameter(
                    type="categorical", choices=[4096, 16384, 65536], default=16384
                ),
            },
            objectives=[
                ObjectiveSpec(
                    metric="throughput_mib_s",
                    unit="MiB/s",
                    direction=Direction.MAXIMIZE,
                    aggregation=Aggregation.MEDIAN,
                    comparison=Comparison.RELATIVE,
                    epsilon=0.0,
                    weight=1.0,
                    minimum_samples=3,
                ),
                ObjectiveSpec(
                    metric="compression_ratio",
                    unit="ratio",
                    direction=Direction.MINIMIZE,
                    aggregation=Aggregation.MEAN,
                    comparison=Comparison.RELATIVE,
                    epsilon=0.0,
                    weight=0.5,
                    minimum_samples=3,
                ),
            ],
            gates=[
                GateSpec(
                    id="roundtrip",
                    kind=GateKind.CORRECTNESS,
                    scope=GateScope.CANDIDATE,
                    metric="roundtrip_ok",
                    operator=Operator.TRUE,
                    hard=True,
                )
            ],
            design=ExperimentalDesign(
                warmup_runs=1,
                min_repeats=3,
                max_repeats=3,
                max_retries=1,
                baseline_every_n=4,
                cooldown_seconds=0,
                confidence_level=0.95,
                bootstrap_resamples=500,
                tail_min_samples=100,
                random_seed=20260301,
            ),
            budget=BudgetSpec(max_candidates=5, max_attempts=40, wall_time_seconds=1800),
            optimizer=OptimizerSpec(type="random", seed=20260301),
        ),
    )


def _validate_experiment_spec(
    session: Session, request: ExperimentCreate
) -> tuple[BenchmarkRecord, ExperimentSpec]:
    spec = request.spec
    benchmark = get_benchmark(session, spec.benchmark_id, spec.benchmark_version)
    if benchmark is None:
        raise SchedulerError("benchmark version is not installed")
    manifest = benchmark.manifest_json
    manifest_parameters = manifest["spec"]["parameters"]
    adapter = manifest["spec"].get("adapter") or {}
    declared_inputs = {item["id"]: item for item in adapter.get("inputs", [])}
    unknown_inputs = sorted(set(spec.input_bindings) - set(declared_inputs))
    if unknown_inputs:
        raise SchedulerError(f"unknown benchmark input bindings: {unknown_inputs}")
    missing_inputs = sorted(
        input_id
        for input_id, declaration in declared_inputs.items()
        if declaration.get("required") and input_id not in spec.input_bindings
    )
    if missing_inputs:
        raise SchedulerError(f"required benchmark inputs are not bound: {missing_inputs}")
    for input_id, binding in spec.input_bindings.items():
        declaration = declared_inputs[input_id]
        if binding.kind != declaration["kind"]:
            raise SchedulerError(f"benchmark input {input_id!r} has a kind mismatch")
        if declaration.get("digestRequired") and binding.digest is None:
            raise SchedulerError(f"benchmark input {input_id!r} requires a sha256 digest")
    if spec.mode == ExperimentMode.OPTIMIZATION:
        unknown_parameters = set(spec.search_space) - set(manifest_parameters)
        if unknown_parameters:
            raise SchedulerError(f"unknown benchmark parameters: {sorted(unknown_parameters)}")
        if set(spec.baseline_parameters) != set(spec.search_space):
            raise SchedulerError(
                "baseline parameters must exactly match the experiment search space"
            )
    else:
        manifest_scenario = manifest["spec"].get("scenario")
        if manifest_scenario is None or spec.scenario is None:
            raise SchedulerError("selection study requires an installed scenario benchmark")
        installed_scenario = ScenarioBenchmarkSpec.model_validate(manifest_scenario)
        if installed_scenario.model_dump(mode="json") != spec.scenario.model_dump(mode="json"):
            raise SchedulerError("selection scenario must match the installed manifest")

    workload_ids = {item["id"] for item in manifest["spec"]["workloads"]}
    selected_workloads = set(spec.workload_ids or workload_ids)
    if not selected_workloads or not selected_workloads <= workload_ids:
        raise SchedulerError("experiment contains an unknown workload")
    for target_id in spec.target_ids:
        target = session.get(TargetRecord, target_id)
        if target is None:
            raise SchedulerError(f"target {target_id!r} does not exist")
        if target.lifecycle_status != "active":
            raise SchedulerError(
                f"target {target_id!r} is {target.lifecycle_status} and cannot be selected"
            )
        if spec.mode == ExperimentMode.OPTIMIZATION and not target.runnable:
            raise SchedulerError(f"target {target_id!r} is inventory-only")
    metric_specs = manifest["spec"]["metrics"]
    for objective in spec.objectives:
        metric = metric_specs.get(objective.metric)
        if metric is None:
            raise SchedulerError(f"objective metric {objective.metric!r} is not declared")
        if metric["unit"] != objective.unit:
            raise SchedulerError(f"objective metric {objective.metric!r} has a unit mismatch")
        if metric["direction"] != objective.direction.value:
            raise SchedulerError(f"objective metric {objective.metric!r} has a direction mismatch")
    return benchmark, spec


def create_experiment(session: Session, request: ExperimentCreate) -> ExperimentRecord:
    _validate_experiment_spec(session, request)
    now = utc_now()
    spec_json = request.spec.model_dump(mode="json")
    record = ExperimentRecord(
        id=new_id("exp"),
        project_id=request.project_id,
        name=request.name,
        description=request.description,
        status=ExperimentStatus.DRAFT,
        spec_json=spec_json,
        spec_digest=canonical_digest(spec_json),
        revision=1,
        optimizer_state_json={"next_sequence": 0, "exhausted": False},
        created_at=now,
        updated_at=now,
    )
    session.add(record)
    session.flush()
    append_event(
        session,
        experiment_id=record.id,
        event_type="experiment.created",
        entity_type="experiment",
        entity_id=record.id,
        idempotency_key=f"experiment.created:{record.id}",
        payload={"spec_digest": record.spec_digest},
    )
    return record


def _manifest_workloads(benchmark: BenchmarkRecord) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in benchmark.manifest_json["spec"]["workloads"]}


def _attempt_count(session: Session, experiment_id: str) -> int:
    return int(
        session.scalar(
            select(func.count(AttemptRecord.id)).where(AttemptRecord.experiment_id == experiment_id)
        )
        or 0
    )


def _rotate(items: list[str], offset: int) -> list[str]:
    if not items:
        return []
    index = offset % len(items)
    return items[index:] + items[:index]


def _create_candidate(
    session: Session,
    experiment: ExperimentRecord,
    parameters: dict[str, Any],
    *,
    role: str,
    sequence: int,
) -> CandidateRecord:
    spec = ExperimentSpec.model_validate(experiment.spec_json)
    benchmark = get_benchmark(session, spec.benchmark_id, spec.benchmark_version)
    if benchmark is None:
        raise SchedulerError("benchmark disappeared while scheduling")
    config_payload = {
        "benchmark": benchmark.manifest_digest,
        "parameters": parameters,
    }
    candidate = CandidateRecord(
        id=new_id("cand"),
        experiment_id=experiment.id,
        sequence=sequence,
        role=role,
        parameters_json=parameters,
        config_digest=canonical_digest(config_payload),
        status=CandidateStatus.PENDING,
        created_at=utc_now(),
    )
    session.add(candidate)
    session.flush()

    workloads = _manifest_workloads(benchmark)
    selected_workloads = spec.workload_ids or list(workloads)
    evaluations: dict[tuple[str, str], EvaluationRecord] = {}
    for workload_id in selected_workloads:
        for target_id in spec.target_ids:
            target = session.get(TargetRecord, target_id)
            if target is None:
                raise SchedulerError(f"target {target_id!r} disappeared")
            target_snapshot = {
                "snapshotDigest": target.snapshot_digest,
                "provider": target.provider,
                "capabilities": target.capabilities_json,
                "fingerprint": target.fingerprint_json,
                "inventory": target.inventory_json,
            }
            evaluation = EvaluationRecord(
                id=new_id("eval"),
                experiment_id=experiment.id,
                candidate_id=candidate.id,
                workload_id=workload_id,
                target_id=target.id,
                target_snapshot_digest=target.snapshot_digest,
                target_snapshot_json=target_snapshot,
                status=CandidateStatus.PENDING,
                created_at=utc_now(),
            )
            session.add(evaluation)
            session.flush()
            evaluations[(workload_id, target_id)] = evaluation
            if spec.mode == ExperimentMode.OPTIMIZATION:
                for repeat_index in range(spec.design.min_repeats):
                    _create_attempt(session, experiment, evaluation, repeat_index, retry_index=0)
    if spec.mode == ExperimentMode.SELECTION:
        if spec.selection is None:
            raise SchedulerError("selection design disappeared while scheduling")
        load_search = spec.scenario.load_search if spec.scenario is not None else None
        if load_search is not None:
            reference = spec.selection.reference_offered_load
            if reference is None:
                raise SchedulerError("selection frontier requires a reference offered load")
            for workload_id in selected_workloads:
                for fraction in load_search.common_load_fractions:
                    _create_selection_load_point(
                        session,
                        experiment,
                        workload_id=workload_id,
                        offered_load=reference * fraction,
                        origin="initial",
                        required_repeats=load_search.boundary_repeats,
                    )
        else:
            by_placement: dict[str, list[str]] = defaultdict(list)
            for binding in spec.selection.target_bindings:
                by_placement[binding.placement_pair_id].append(binding.target_id)
            rng = Random(spec.selection.random_seed + sequence)
            placement_order = sorted(by_placement)
            rng.shuffle(placement_order)
            target_orders: dict[str, list[str]] = {}
            for placement_id, target_ids in by_placement.items():
                target_orders[placement_id] = sorted(target_ids)
                rng.shuffle(target_orders[placement_id])
            for workload_id in selected_workloads:
                for repeat_index in range(spec.design.min_repeats):
                    rotated_placements = _rotate(placement_order, repeat_index)
                    for placement_id in rotated_placements:
                        ordered_targets = _rotate(target_orders[placement_id], repeat_index)
                        for target_id in ordered_targets:
                            _create_attempt(
                                session,
                                experiment,
                                evaluations[(workload_id, target_id)],
                                repeat_index,
                                retry_index=0,
                            )
    append_event(
        session,
        experiment_id=experiment.id,
        event_type="candidate.created",
        entity_type="candidate",
        entity_id=candidate.id,
        idempotency_key=f"candidate.created:{candidate.id}",
        payload={
            "sequence": sequence,
            "role": role,
            "config_digest": candidate.config_digest,
            "parameters": parameters,
        },
    )
    return candidate


def _next_queue_sequence(session: Session, experiment_id: str) -> int:
    maximum = session.scalar(
        select(func.max(AttemptRecord.queue_sequence)).where(
            AttemptRecord.experiment_id == experiment_id
        )
    )
    return int(maximum) + 1 if maximum is not None else 0


def _create_attempt(
    session: Session,
    experiment: ExperimentRecord,
    evaluation: EvaluationRecord,
    repeat_index: int,
    retry_index: int,
    *,
    load_point: SelectionLoadPointRecord | None = None,
) -> AttemptRecord:
    identity = (
        f"{evaluation.id}:{load_point.id}:{repeat_index}:{retry_index}"
        if load_point is not None
        else f"{evaluation.id}:{repeat_index}:{retry_index}"
    )
    attempt = AttemptRecord(
        id=new_id("att"),
        experiment_id=experiment.id,
        evaluation_id=evaluation.id,
        selection_load_point_id=load_point.id if load_point is not None else None,
        repeat_index=repeat_index,
        retry_index=retry_index,
        queue_sequence=_next_queue_sequence(session, experiment.id),
        status=AttemptStatus.QUEUED,
        fencing_token=0,
        idempotency_key=identity,
        created_at=utc_now(),
    )
    session.add(attempt)
    session.flush()
    append_event(
        session,
        experiment_id=experiment.id,
        event_type="attempt.queued",
        entity_type="attempt",
        entity_id=attempt.id,
        idempotency_key=f"attempt.queued:{attempt.id}",
        payload={
            "repeat_index": repeat_index,
            "retry_index": retry_index,
            "queue_sequence": attempt.queue_sequence,
            "selection_load_point_id": attempt.selection_load_point_id,
        },
    )
    return attempt


def _canonical_offered_load(value: float | Decimal) -> tuple[Decimal, str]:
    offered_load = Decimal(str(value)).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)
    if offered_load <= 0:
        raise SchedulerError("offered load must be positive")
    key = format(offered_load.normalize(), "f")
    return offered_load, key


def _enqueue_selection_load_point(
    session: Session,
    experiment: ExperimentRecord,
    point: SelectionLoadPointRecord,
) -> None:
    spec = ExperimentSpec.model_validate(experiment.spec_json)
    if spec.selection is None:
        raise SchedulerError("selection design disappeared while scheduling")
    evaluations = list(
        session.scalars(
            select(EvaluationRecord).where(
                EvaluationRecord.experiment_id == experiment.id,
                EvaluationRecord.workload_id == point.workload_id,
            )
        )
    )
    by_target = {evaluation.target_id: evaluation for evaluation in evaluations}
    if set(by_target) != set(spec.target_ids):
        raise SchedulerError("selection load point has an incomplete evaluation matrix")
    required_attempts = len(evaluations) * point.required_repeats
    existing_attempts = int(
        session.scalar(
            select(func.count(AttemptRecord.id)).where(
                AttemptRecord.selection_load_point_id == point.id
            )
        )
        or 0
    )
    projected_attempts = (
        _attempt_count(session, experiment.id) + required_attempts - existing_attempts
    )
    if projected_attempts > spec.budget.max_attempts:
        raise SchedulerError("selection frontier attempt budget is exhausted")

    by_placement: dict[str, list[str]] = defaultdict(list)
    for binding in spec.selection.target_bindings:
        by_placement[binding.placement_pair_id].append(binding.target_id)
    rng = Random(spec.selection.random_seed + point.sequence)
    placement_order = sorted(by_placement)
    rng.shuffle(placement_order)
    target_orders: dict[str, list[str]] = {}
    for placement_id, target_ids in by_placement.items():
        target_orders[placement_id] = sorted(target_ids)
        rng.shuffle(target_orders[placement_id])
    for repeat_index in range(point.required_repeats):
        for placement_id in _rotate(placement_order, repeat_index):
            for target_id in _rotate(target_orders[placement_id], repeat_index):
                evaluation = by_target[target_id]
                identity = f"{evaluation.id}:{point.id}:{repeat_index}:0"
                exists = session.scalar(
                    select(AttemptRecord.id).where(AttemptRecord.idempotency_key == identity)
                )
                if exists is None:
                    _create_attempt(
                        session,
                        experiment,
                        evaluation,
                        repeat_index,
                        retry_index=0,
                        load_point=point,
                    )


def _create_selection_load_point(
    session: Session,
    experiment: ExperimentRecord,
    *,
    workload_id: str,
    offered_load: float | Decimal,
    origin: str,
    required_repeats: int,
) -> SelectionLoadPointRecord:
    decimal_load, load_key = _canonical_offered_load(offered_load)
    existing = session.scalar(
        select(SelectionLoadPointRecord).where(
            SelectionLoadPointRecord.experiment_id == experiment.id,
            SelectionLoadPointRecord.workload_id == workload_id,
            SelectionLoadPointRecord.offered_load_key == load_key,
        )
    )
    if existing is not None:
        _enqueue_selection_load_point(session, experiment, existing)
        return existing
    maximum_sequence = session.scalar(
        select(func.max(SelectionLoadPointRecord.sequence)).where(
            SelectionLoadPointRecord.experiment_id == experiment.id
        )
    )
    sequence = int(maximum_sequence) + 1 if maximum_sequence is not None else 0
    point = SelectionLoadPointRecord(
        id=new_id("load"),
        experiment_id=experiment.id,
        workload_id=workload_id,
        sequence=sequence,
        offered_load=decimal_load,
        offered_load_key=load_key,
        origin=origin,
        required_repeats=required_repeats,
        status="queued",
        analysis_json={},
        created_at=utc_now(),
    )
    session.add(point)
    session.flush()
    _enqueue_selection_load_point(session, experiment, point)
    append_event(
        session,
        experiment_id=experiment.id,
        event_type="selection.load_point.queued",
        entity_type="selection_load_point",
        entity_id=point.id,
        idempotency_key=f"selection.load_point.queued:{point.id}",
        payload={
            "workload_id": workload_id,
            "offered_load": float(decimal_load),
            "origin": origin,
            "required_repeats": required_repeats,
        },
    )
    return point


def _optimizer_history(session: Session, experiment: ExperimentRecord) -> list[dict[str, Any]]:
    snapshot = session.scalar(
        select(AnalysisSnapshotRecord)
        .where(AnalysisSnapshotRecord.experiment_id == experiment.id)
        .order_by(AnalysisSnapshotRecord.created_at.desc())
        .limit(1)
    )
    if snapshot is None:
        return []
    result = snapshot.result_json
    history: list[dict[str, Any]] = []
    for candidate in result.get("candidates", []):
        if candidate.get("role") == "baseline":
            continue
        values = [objective.get("raw") for objective in candidate.get("objectives", [])]
        if not candidate.get("feasible") or any(value is None for value in values):
            values = None
        history.append(
            {
                "id": candidate["id"],
                "parameters": candidate.get("parameters", {}),
                "values": values,
            }
        )
    return history


def _schedule_next_candidate(
    session: Session, experiment: ExperimentRecord
) -> CandidateRecord | None:
    spec = ExperimentSpec.model_validate(experiment.spec_json)
    candidates = list(
        session.scalars(
            select(CandidateRecord)
            .where(CandidateRecord.experiment_id == experiment.id)
            .order_by(CandidateRecord.sequence)
        )
    )
    if len(candidates) >= spec.budget.max_candidates:
        return None
    required_attempts = len(spec.target_ids) * len(spec.workload_ids or ["default"])
    required_attempts *= spec.design.min_repeats
    if _attempt_count(session, experiment.id) + required_attempts > spec.budget.max_attempts:
        return None
    existing = [{"parameters": candidate.parameters_json} for candidate in candidates]
    try:
        parameters = suggest_candidate(
            spec.search_space,
            spec.optimizer,
            sequence=len(candidates),
            existing=existing,
            objective_directions=[objective.direction for objective in spec.objectives],
            history=_optimizer_history(session, experiment),
        )
    except SearchSpaceExhausted:
        experiment.optimizer_state_json = {
            **experiment.optimizer_state_json,
            "exhausted": True,
        }
        return None
    return _create_candidate(
        session,
        experiment,
        parameters,
        role="candidate",
        sequence=len(candidates),
    )


def _require_start_readiness(
    session: Session, experiment: ExperimentRecord, spec: ExperimentSpec
) -> None:
    benchmark = get_benchmark(session, spec.benchmark_id, spec.benchmark_version)
    if benchmark is None:
        raise SchedulerError("benchmark version is not installed")
    execution_status = (
        benchmark.manifest_json["spec"].get("x-extensions", {}).get("executionStatus", "executable")
    )
    if execution_status != "executable":
        raise SchedulerError(
            f"benchmark is {execution_status}; Stage 0 adapters cannot execute workloads"
        )
    runtime = benchmark.manifest_json["spec"]["runtime"]
    image = runtime.get("image")
    if runtime.get("type") == "container" and (
        not isinstance(image, str)
        or re.fullmatch(
            r"[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?@sha256:[0-9a-f]{64}", image
        )
        is None
    ):
        raise SchedulerError("container benchmarks require an image pinned by sha256 digest")
    if (
        spec.mode == ExperimentMode.SELECTION
        and spec.scenario is not None
        and spec.scenario.load_search is not None
        and (spec.selection is None or spec.selection.reference_offered_load is None)
    ):
        raise SchedulerError("selection frontier requires a reference offered load")
    required_capabilities = set(benchmark.manifest_json["spec"].get("capabilities", []))
    for target_id in spec.target_ids:
        target = session.get(TargetRecord, target_id)
        if target is None or not target.runnable:
            raise SchedulerError(f"target {target_id!r} has no runnable worker")
        missing_capabilities = sorted(required_capabilities - set(target.capabilities_json))
        if missing_capabilities:
            raise SchedulerError(
                f"target {target_id!r} lacks benchmark capabilities: {missing_capabilities}"
            )


def start_experiment(session: Session, experiment: ExperimentRecord) -> ExperimentRecord:
    spec = ExperimentSpec.model_validate(experiment.spec_json)
    if ExperimentStatus(experiment.status) == ExperimentStatus.PAUSED:
        return resume_experiment(session, experiment)
    require_experiment_transition(experiment.status, ExperimentStatus.QUEUED)
    _require_start_readiness(session, experiment, spec)
    if not session.scalar(
        select(CandidateRecord.id).where(CandidateRecord.experiment_id == experiment.id).limit(1)
    ):
        _create_candidate(
            session,
            experiment,
            spec.baseline_parameters,
            role="scenario" if spec.mode == ExperimentMode.SELECTION else "baseline",
            sequence=0,
        )
        if spec.mode == ExperimentMode.OPTIMIZATION and spec.budget.max_candidates > 1:
            _schedule_next_candidate(session, experiment)
    experiment.status = ExperimentStatus.QUEUED
    experiment.updated_at = utc_now()
    append_event(
        session,
        experiment_id=experiment.id,
        event_type="experiment.queued",
        entity_type="experiment",
        entity_id=experiment.id,
        idempotency_key=f"experiment.queued:{experiment.id}:{experiment.revision}",
    )
    return experiment


def pause_experiment(session: Session, experiment: ExperimentRecord) -> ExperimentRecord:
    require_experiment_transition(experiment.status, ExperimentStatus.PAUSED)
    experiment.status = ExperimentStatus.PAUSED
    experiment.revision += 1
    experiment.updated_at = utc_now()
    append_event(
        session,
        experiment_id=experiment.id,
        event_type="experiment.paused",
        entity_type="experiment",
        entity_id=experiment.id,
        idempotency_key=f"experiment.paused:{experiment.id}:{experiment.revision}",
    )
    return experiment


def resume_experiment(session: Session, experiment: ExperimentRecord) -> ExperimentRecord:
    spec = ExperimentSpec.model_validate(experiment.spec_json)
    _require_start_readiness(session, experiment, spec)
    require_experiment_transition(experiment.status, ExperimentStatus.QUEUED)
    experiment.status = ExperimentStatus.QUEUED
    experiment.revision += 1
    experiment.updated_at = utc_now()
    append_event(
        session,
        experiment_id=experiment.id,
        event_type="experiment.resumed",
        entity_type="experiment",
        entity_id=experiment.id,
        idempotency_key=f"experiment.resumed:{experiment.id}:{experiment.revision}",
    )
    return experiment


def cancel_experiment(session: Session, experiment: ExperimentRecord) -> ExperimentRecord:
    require_experiment_transition(experiment.status, ExperimentStatus.CANCELLED)
    now = utc_now()
    experiment.status = ExperimentStatus.CANCELLED
    experiment.revision += 1
    experiment.updated_at = now
    experiment.finished_at = now
    queued = list(
        session.scalars(
            select(AttemptRecord).where(
                AttemptRecord.experiment_id == experiment.id,
                AttemptRecord.status == AttemptStatus.QUEUED,
            )
        )
    )
    for attempt in queued:
        attempt.status = AttemptStatus.CANCELLED
        attempt.completed_at = now
    append_event(
        session,
        experiment_id=experiment.id,
        event_type="experiment.cancelled",
        entity_type="experiment",
        entity_id=experiment.id,
        idempotency_key=f"experiment.cancelled:{experiment.id}:{experiment.revision}",
    )
    return experiment


def retry_attempt(session: Session, attempt: AttemptRecord) -> AttemptRecord:
    if AttemptStatus(attempt.status) not in TERMINAL_ATTEMPTS:
        raise SchedulerError("only terminal attempts can be retried")
    experiment = session.get(ExperimentRecord, attempt.experiment_id)
    evaluation = session.get(EvaluationRecord, attempt.evaluation_id)
    if experiment is None or evaluation is None:
        raise SchedulerError("attempt parents are missing")
    spec = ExperimentSpec.model_validate(experiment.spec_json)
    latest_retry = int(
        session.scalar(
            select(func.max(AttemptRecord.retry_index)).where(
                AttemptRecord.evaluation_id == attempt.evaluation_id,
                AttemptRecord.selection_load_point_id == attempt.selection_load_point_id,
                AttemptRecord.repeat_index == attempt.repeat_index,
            )
        )
        or 0
    )
    if latest_retry >= spec.design.max_retries:
        raise SchedulerError("retry budget is exhausted")
    evaluation.status = CandidateStatus.PENDING
    candidate = session.get(CandidateRecord, evaluation.candidate_id)
    if candidate:
        candidate.status = CandidateStatus.PENDING
    load_point = (
        session.get(SelectionLoadPointRecord, attempt.selection_load_point_id)
        if attempt.selection_load_point_id is not None
        else None
    )
    return _create_attempt(
        session,
        experiment,
        evaluation,
        attempt.repeat_index,
        retry_index=latest_retry + 1,
        load_point=load_point,
    )


def _successful_attempt_for_repeat(
    attempts: list[AttemptRecord], repeat_index: int
) -> AttemptRecord | None:
    successful = [
        attempt
        for attempt in attempts
        if attempt.repeat_index == repeat_index
        and AttemptStatus(attempt.status) == AttemptStatus.SUCCEEDED
    ]
    return successful[-1] if successful else None


def _frontier_point_for_target(
    session: Session,
    point: SelectionLoadPointRecord,
    evaluation: EvaluationRecord,
) -> FrontierPointEvidence:
    attempts = list(
        session.scalars(
            select(AttemptRecord)
            .where(
                AttemptRecord.selection_load_point_id == point.id,
                AttemptRecord.evaluation_id == evaluation.id,
            )
            .order_by(AttemptRecord.repeat_index, AttemptRecord.retry_index)
        )
    )
    blocks = []
    for repeat_index in range(point.required_repeats):
        attempt = _successful_attempt_for_repeat(attempts, repeat_index)
        if attempt is None:
            raise SchedulerError("frontier point is missing a successful repeat")
        observations = list(
            session.scalars(
                select(ObservationRecord).where(ObservationRecord.attempt_id == attempt.id)
            )
        )
        checks = list(
            session.scalars(select(CheckRecord).where(CheckRecord.attempt_id == attempt.id))
        )
        metrics = [
            {
                "metric": observation.metric,
                "value": observation.value_boolean
                if observation.value_boolean is not None
                else observation.value_number,
                "sample_count": observation.sample_count,
            }
            for observation in observations
            if observation.phase != "warmup"
        ]
        result = {
            "status": "succeeded",
            "metrics": metrics,
            "checks": [
                {"id": check.check_id, "passed": check.passed, "kind": check.kind}
                for check in checks
            ],
        }
        extensions = (attempt.envelope_json or {}).get("extensions", {})
        blocks.append(
            frontier_block_from_scenario_result(
                result,
                block_id=attempt.id,
                time_block_id=str(
                    extensions.get("timeBlockId")
                    or f"{point.id}:{repeat_index}:{evaluation.target_id}"
                ),
            )
        )
    return FrontierPointEvidence(offered_load=float(point.offered_load), blocks=blocks)


def _reconcile_selection_point(
    session: Session,
    experiment: ExperimentRecord,
    point: SelectionLoadPointRecord,
    spec: ExperimentSpec,
) -> bool:
    evaluations = list(
        session.scalars(
            select(EvaluationRecord).where(
                EvaluationRecord.experiment_id == experiment.id,
                EvaluationRecord.workload_id == point.workload_id,
            )
        )
    )
    ready = True
    exhausted: list[str] = []
    for evaluation in evaluations:
        attempts = list(
            session.scalars(
                select(AttemptRecord)
                .where(
                    AttemptRecord.selection_load_point_id == point.id,
                    AttemptRecord.evaluation_id == evaluation.id,
                )
                .order_by(AttemptRecord.repeat_index, AttemptRecord.retry_index)
            )
        )
        for repeat_index in range(point.required_repeats):
            repeated = [item for item in attempts if item.repeat_index == repeat_index]
            if _successful_attempt_for_repeat(repeated, repeat_index) is not None:
                continue
            if any(AttemptStatus(item.status) not in TERMINAL_ATTEMPTS for item in repeated):
                ready = False
                continue
            latest = repeated[-1] if repeated else None
            if latest is not None and latest.retry_index < spec.design.max_retries:
                _create_attempt(
                    session,
                    experiment,
                    evaluation,
                    repeat_index,
                    retry_index=latest.retry_index + 1,
                    load_point=point,
                )
                ready = False
            else:
                exhausted.append(f"{evaluation.id}:{repeat_index}")
    if exhausted:
        point.status = "unresolved"
        point.analysis_json = {"reason": "repeat_failures_exhausted", "blocks": exhausted}
        point.completed_at = utc_now()
        return True
    if not ready:
        point.status = "running"
        return False
    point.status = "complete"
    point.completed_at = point.completed_at or utc_now()
    point.analysis_input_digest = canonical_digest(
        {
            "point": point.id,
            "attempts": sorted(
                attempt.id
                for attempt in session.scalars(
                    select(AttemptRecord).where(
                        AttemptRecord.selection_load_point_id == point.id,
                        AttemptRecord.status == AttemptStatus.SUCCEEDED,
                    )
                )
            ),
        }
    )
    return True


def _advance_selection_frontier(
    session: Session,
    experiment: ExperimentRecord,
    spec: ExperimentSpec,
) -> bool:
    if spec.scenario is None or spec.selection is None or spec.scenario.load_search is None:
        return False
    protocol = spec.scenario.load_search
    if spec.scenario.goodput is None or spec.scenario.tail_evidence is None:
        raise SchedulerError("frontier scenario requires goodput and tail evidence policies")
    latency_gate = next(
        (
            gate
            for gate in spec.scenario.slo_gates
            if gate.metric == "latency_p99_ms" and gate.threshold is not None
        ),
        None,
    )
    if latency_gate is None:
        raise SchedulerError("frontier scenario requires a latency_p99_ms threshold")
    points = list(
        session.scalars(
            select(SelectionLoadPointRecord)
            .where(SelectionLoadPointRecord.experiment_id == experiment.id)
            .order_by(SelectionLoadPointRecord.sequence)
        )
    )
    if not points:
        raise SchedulerError("selection frontier has no load points")
    all_ready = True
    for point in points:
        if point.status not in {"complete", "unresolved"}:
            all_ready = _reconcile_selection_point(session, experiment, point, spec) and all_ready
    if not all_ready:
        return False
    if any(point.status == "unresolved" for point in points):
        terminal_status = "frontier_unresolved"
        target_frontiers: dict[str, dict[str, Any]] = {}
    else:
        target_frontiers = {}
        next_loads: list[float] = []
        workload_terminal = True
        for workload_id in sorted({point.workload_id for point in points}):
            workload_points = [point for point in points if point.workload_id == workload_id]
            adaptive_points_used = sum(point.origin == "adaptive" for point in workload_points)
            evaluations = list(
                session.scalars(
                    select(EvaluationRecord).where(
                        EvaluationRecord.experiment_id == experiment.id,
                        EvaluationRecord.workload_id == workload_id,
                    )
                )
            )
            for evaluation in evaluations:
                frontier = analyze_slo_frontier(
                    [
                        _frontier_point_for_target(session, point, evaluation)
                        for point in workload_points
                    ],
                    protocol,
                    latency_p99_threshold=float(latency_gate.threshold),
                    goodput=spec.scenario.goodput,
                    tail=spec.scenario.tail_evidence,
                    adaptive_points_used=adaptive_points_used,
                )
                key = f"{workload_id}:{evaluation.target_id}"
                target_frontiers[key] = frontier
                next_load = frontier.get("next_offered_load")
                if isinstance(next_load, (int, float)) and not isinstance(next_load, bool):
                    next_loads.append(float(next_load))
                if frontier["status"] not in {
                    "resolved",
                    "non_monotonic",
                    "frontier_unresolved",
                }:
                    workload_terminal = False
        existing_keys = {point.offered_load_key for point in points}
        next_candidates = [
            value
            for value in sorted(set(next_loads))
            if _canonical_offered_load(value)[1] not in existing_keys
        ]
        if next_candidates:
            for workload_id in sorted({point.workload_id for point in points}):
                _create_selection_load_point(
                    session,
                    experiment,
                    workload_id=workload_id,
                    offered_load=next_candidates[0],
                    origin="adaptive",
                    required_repeats=protocol.boundary_repeats,
                )
            experiment.status = ExperimentStatus.QUEUED
            experiment.updated_at = utc_now()
            return False
        terminal_status = "resolved" if workload_terminal else "frontier_unresolved"

    digest = canonical_digest(target_frontiers)
    for point in points:
        point.analysis_json = {
            "frontier_status": terminal_status,
            "target_frontiers": target_frontiers,
        }
        append_event(
            session,
            experiment_id=experiment.id,
            event_type="selection.load_point.analyzed",
            entity_type="selection_load_point",
            entity_id=point.id,
            idempotency_key=f"selection.load_point.analyzed:{point.id}:{digest}",
            payload={"status": terminal_status, "analysis_digest": digest},
        )
    evaluations = list(
        session.scalars(
            select(EvaluationRecord).where(EvaluationRecord.experiment_id == experiment.id)
        )
    )
    for evaluation in evaluations:
        evaluation.status = (
            CandidateStatus.FEASIBLE
            if terminal_status == "resolved"
            else CandidateStatus.INCONCLUSIVE
        )
        evaluation.completed_at = utc_now()
    return True


def _reconcile_evaluation(
    session: Session, experiment: ExperimentRecord, evaluation: EvaluationRecord
) -> None:
    spec = ExperimentSpec.model_validate(experiment.spec_json)
    attempts = list(
        session.scalars(
            select(AttemptRecord)
            .where(AttemptRecord.evaluation_id == evaluation.id)
            .order_by(AttemptRecord.repeat_index, AttemptRecord.retry_index)
        )
    )
    by_repeat: dict[int, list[AttemptRecord]] = defaultdict(list)
    for attempt in attempts:
        by_repeat[attempt.repeat_index].append(attempt)

    failures_exhausted = False
    completed_repeats = 0
    any_active = False
    for repeat_index in range(spec.design.min_repeats):
        repeated = by_repeat.get(repeat_index, [])
        if any(AttemptStatus(item.status) == AttemptStatus.SUCCEEDED for item in repeated):
            completed_repeats += 1
            continue
        active = [item for item in repeated if AttemptStatus(item.status) not in TERMINAL_ATTEMPTS]
        if active:
            any_active = True
            continue
        latest = repeated[-1] if repeated else None
        if latest and latest.retry_index < spec.design.max_retries:
            _create_attempt(
                session,
                experiment,
                evaluation,
                repeat_index,
                retry_index=latest.retry_index + 1,
            )
            any_active = True
        else:
            failures_exhausted = True

    if completed_repeats == spec.design.min_repeats:
        successful_ids = [
            item.id for item in attempts if AttemptStatus(item.status) == AttemptStatus.SUCCEEDED
        ]
        failed_check = session.scalar(
            select(CheckRecord.id)
            .where(CheckRecord.attempt_id.in_(successful_ids), CheckRecord.passed.is_(False))
            .limit(1)
        )
        evaluation.status = CandidateStatus.INFEASIBLE if failed_check else CandidateStatus.FEASIBLE
        evaluation.completed_at = utc_now()
    elif failures_exhausted and not any_active:
        evaluation.status = CandidateStatus.FAILED
        evaluation.completed_at = utc_now()
    else:
        evaluation.status = CandidateStatus.RUNNING


def _reconcile_candidate(session: Session, candidate: CandidateRecord) -> None:
    evaluations = list(
        session.scalars(
            select(EvaluationRecord).where(EvaluationRecord.candidate_id == candidate.id)
        )
    )
    statuses = {CandidateStatus(item.status) for item in evaluations}
    if statuses and statuses <= TERMINAL_CANDIDATES:
        if CandidateStatus.FAILED in statuses:
            candidate.status = CandidateStatus.FAILED
        elif CandidateStatus.INFEASIBLE in statuses:
            candidate.status = CandidateStatus.INFEASIBLE
        elif CandidateStatus.INCONCLUSIVE in statuses:
            candidate.status = CandidateStatus.INCONCLUSIVE
        else:
            candidate.status = CandidateStatus.FEASIBLE
        candidate.completed_at = utc_now()
    elif any(status == CandidateStatus.RUNNING for status in statuses):
        candidate.status = CandidateStatus.RUNNING


def advance_experiment(session: Session, experiment_id: str) -> None:
    experiment = session.get(ExperimentRecord, experiment_id)
    if experiment is None or ExperimentStatus(experiment.status) in {
        ExperimentStatus.CANCELLED,
        ExperimentStatus.FAILED,
        ExperimentStatus.COMPLETED,
    }:
        return
    spec = ExperimentSpec.model_validate(experiment.spec_json)
    evaluations = list(
        session.scalars(
            select(EvaluationRecord).where(EvaluationRecord.experiment_id == experiment.id)
        )
    )
    frontier_selection = (
        spec.mode == ExperimentMode.SELECTION
        and spec.scenario is not None
        and spec.scenario.load_search is not None
    )
    frontier_terminal = False
    if frontier_selection:
        frontier_terminal = _advance_selection_frontier(session, experiment, spec)
    else:
        for evaluation in evaluations:
            _reconcile_evaluation(session, experiment, evaluation)
    candidates = list(
        session.scalars(
            select(CandidateRecord)
            .where(CandidateRecord.experiment_id == experiment.id)
            .order_by(CandidateRecord.sequence)
        )
    )
    for candidate in candidates:
        _reconcile_candidate(session, candidate)
    session.flush()

    if ExperimentStatus(experiment.status) == ExperimentStatus.PAUSED:
        return
    if frontier_selection and not frontier_terminal:
        return
    anchor_role = "scenario" if spec.mode == ExperimentMode.SELECTION else "baseline"
    anchor = next((item for item in candidates if item.role == anchor_role), None)
    active = [
        item for item in candidates if CandidateStatus(item.status) not in TERMINAL_CANDIDATES
    ]
    if anchor and CandidateStatus(anchor.status) in TERMINAL_CANDIDATES and not active:
        from looper_api.analysis_service import build_analysis_snapshot

        snapshot = build_analysis_snapshot(session, experiment.id, persist=True)
        by_id = {item["id"]: item for item in snapshot["candidates"]}
        for candidate in candidates:
            result = by_id.get(candidate.id)
            if result and not result.get("feasible", False):
                candidate.status = (
                    CandidateStatus.INCONCLUSIVE
                    if result.get("status") == "inconclusive"
                    else CandidateStatus.INFEASIBLE
                )
                candidate.infeasible_reason = result.get("reason")
        session.flush()

        can_schedule = (
            spec.mode == ExperimentMode.OPTIMIZATION
            and len(candidates) < spec.budget.max_candidates
            and not (experiment.optimizer_state_json.get("exhausted", False))
        )
        if can_schedule and _schedule_next_candidate(session, experiment) is not None:
            experiment.status = ExperimentStatus.QUEUED
            experiment.updated_at = utc_now()
            return

        now = utc_now()
        experiment.status = ExperimentStatus.COMPLETED
        experiment.finished_at = now
        experiment.updated_at = now
        experiment.revision += 1
        append_event(
            session,
            experiment_id=experiment.id,
            event_type="experiment.completed",
            entity_type="experiment",
            entity_id=experiment.id,
            idempotency_key=f"experiment.completed:{experiment.id}",
            payload={"candidate_count": len(candidates)},
        )


def mark_experiment_started(session: Session, experiment: ExperimentRecord) -> None:
    if ExperimentStatus(experiment.status) == ExperimentStatus.QUEUED:
        experiment.status = ExperimentStatus.RUNNING
        now = utc_now()
        experiment.started_at = experiment.started_at or now
        experiment.updated_at = now
        append_event(
            session,
            experiment_id=experiment.id,
            event_type="experiment.started",
            entity_type="experiment",
            entity_id=experiment.id,
            idempotency_key=f"experiment.started:{experiment.id}",
        )
