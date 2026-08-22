from __future__ import annotations

from typing import Any, Literal

from looper_core.canonical import canonical_digest, utc_now
from looper_core.contracts import ExperimentCreate, ExperimentMode, ExperimentSpec
from looper_core.state import ExperimentStatus
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from looper_api.analysis_service import build_analysis_snapshot
from looper_api.events import append_event
from looper_api.models import BenchmarkRecord, CandidateRecord, ExperimentRecord
from looper_api.scheduler import SchedulerError, create_experiment, start_experiment
from looper_api.serialization import experiment_view


class PostBenchmarkAction(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9._-]*$")
    label: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)
    risk: Literal["low", "medium", "high"] = "low"
    apply_mode: Literal["benchmark-parameter"] = Field(
        default="benchmark-parameter", alias="applyMode"
    )
    parameter: str = Field(min_length=1, max_length=100)
    value: Any
    minimum_improvement_ratio: float = Field(
        default=0.05, alias="minimumImprovementRatio", ge=0
    )
    guard_metric: str | None = Field(default=None, alias="guardMetric", min_length=1)
    maximum_guard_regression_ratio: float | None = Field(
        default=None, alias="maximumGuardRegressionRatio", ge=0
    )


def _benchmark(session: Session, spec: ExperimentSpec) -> BenchmarkRecord:
    benchmark = session.scalar(
        select(BenchmarkRecord).where(
            BenchmarkRecord.benchmark_id == spec.benchmark_id,
            BenchmarkRecord.version == spec.benchmark_version,
        )
    )
    if benchmark is None:
        raise SchedulerError("experiment benchmark is no longer installed")
    return benchmark


def _action_declarations(benchmark: BenchmarkRecord) -> list[PostBenchmarkAction]:
    extensions = benchmark.manifest_json["spec"].get("x-extensions", {})
    raw_actions = extensions.get("postBenchmarkActions", [])
    if not isinstance(raw_actions, list):
        raise SchedulerError("postBenchmarkActions must be a list")
    try:
        return [PostBenchmarkAction.model_validate(item) for item in raw_actions]
    except ValueError as error:
        raise SchedulerError(f"post-Benchmark action declaration is invalid: {error}") from error


def _value_allowed(declaration: dict[str, Any], value: Any) -> bool:
    parameter_type = declaration.get("type")
    if parameter_type == "boolean":
        return isinstance(value, bool)
    if parameter_type == "categorical":
        return value in declaration.get("choices", [])
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if parameter_type == "integer" and not isinstance(value, int):
        return False
    minimum = declaration.get("minimum")
    maximum = declaration.get("maximum")
    if minimum is not None and value < minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    step = declaration.get("step")
    if step is not None and minimum is not None:
        quotient = (float(value) - float(minimum)) / float(step)
        if abs(quotient - round(quotient)) > 1e-9:
            return False
    return True


def _best_candidate(
    session: Session, experiment: ExperimentRecord, spec: ExperimentSpec
) -> tuple[dict[str, Any], dict[str, Any]]:
    analysis = build_analysis_snapshot(session, experiment.id, persist=False)
    primary = spec.objectives[0]
    scored: list[tuple[float, dict[str, Any]]] = []
    for candidate in analysis.get("candidates", []):
        if not candidate.get("feasible"):
            continue
        objective = next(
            (
                item
                for item in candidate.get("objectives", [])
                if item.get("metric") == primary.metric
            ),
            None,
        )
        value = objective.get("raw") if objective else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            scored.append((float(value), candidate))
    if not scored:
        raise SchedulerError("completed experiment has no feasible candidate to optimize")
    scored.sort(key=lambda item: item[0], reverse=primary.direction.value == "maximize")
    return scored[0][1], analysis


def _recommend_action(
    session: Session,
    experiment: ExperimentRecord,
    spec: ExperimentSpec,
    benchmark: BenchmarkRecord,
) -> tuple[PostBenchmarkAction, dict[str, Any], dict[str, Any]]:
    best, analysis = _best_candidate(session, experiment, spec)
    current = dict(best.get("parameters", {}))
    if not current:
        raise SchedulerError("best candidate has no tunable parameters")
    declarations = benchmark.manifest_json["spec"].get("parameters", {})
    evaluated = {
        canonical_digest(item.parameters_json)
        for item in session.scalars(
            select(CandidateRecord).where(CandidateRecord.experiment_id == experiment.id)
        )
    }
    for action in _action_declarations(benchmark):
        if action.risk != "low":
            continue
        declaration = declarations.get(action.parameter)
        if not isinstance(declaration, dict) or action.parameter not in current:
            continue
        if not _value_allowed(declaration, action.value):
            raise SchedulerError(
                f"action {action.id!r} proposes an invalid value for {action.parameter!r}"
            )
        candidate = {**current, action.parameter: action.value}
        if candidate == current or canonical_digest(candidate) in evaluated:
            continue
        return action, current, analysis
    raise SchedulerError("no unevaluated low-risk post-Benchmark action is available")


def _follow_up_spec(
    source: ExperimentSpec,
    baseline_parameters: dict[str, Any],
    action: PostBenchmarkAction,
) -> ExperimentSpec:
    payload = source.model_dump(mode="json")
    payload["mode"] = ExperimentMode.OPTIMIZATION
    payload["baseline_parameters"] = baseline_parameters
    payload["search_space"] = {
        name: {
            "type": "categorical",
            "choices": [value, action.value]
            if name == action.parameter and value != action.value
            else [value],
            "default": value,
        }
        for name, value in baseline_parameters.items()
    }
    payload["scenario"] = None
    payload["selection"] = None
    repeats = max(3, source.design.min_repeats)
    workload_count = max(1, len(source.workload_ids))
    payload["design"] = {
        **payload["design"],
        "min_repeats": repeats,
        "max_repeats": repeats,
        "baseline_every_n": 1,
        "random_seed": source.design.random_seed + 7919,
    }
    payload["budget"] = {
        "max_candidates": 2,
        "max_attempts": 2 * repeats * len(source.target_ids) * workload_count,
        "wall_time_seconds": source.budget.wall_time_seconds,
    }
    payload["optimizer"] = {"type": "grid", "seed": source.optimizer.seed + 7919}
    return ExperimentSpec.model_validate(payload)


def _linked_state(experiment: ExperimentRecord) -> dict[str, Any] | None:
    value = experiment.optimizer_state_json.get("post_optimization")
    return dict(value) if isinstance(value, dict) else None


def _objective_by_metric(candidate: dict[str, Any], metric: str) -> dict[str, Any] | None:
    return next(
        (item for item in candidate.get("objectives", []) if item.get("metric") == metric),
        None,
    )


def _completed_decision(
    session: Session, child: ExperimentRecord, link: dict[str, Any]
) -> tuple[str, str, dict[str, Any] | None]:
    analysis = build_analysis_snapshot(session, child.id, persist=False)
    candidate = next(
        (item for item in analysis.get("candidates", []) if item.get("role") == "candidate"),
        None,
    )
    if candidate is None:
        return "inconclusive", "复测没有产生候选结果，保留原配置。", None
    if not candidate.get("feasible"):
        return "rolled_back", "候选未通过正确性或证据门槛，保留原配置。", candidate
    primary = _objective_by_metric(candidate, str(link["primaryMetric"]))
    if primary is None or primary.get("status") != "available":
        return "inconclusive", "主指标证据不足，保留原配置。", candidate
    threshold = float(link["minimumImprovementRatio"])
    lower = primary.get("lower")
    upper = primary.get("upper")
    if not isinstance(lower, (int, float)) or not isinstance(upper, (int, float)):
        return "inconclusive", "主指标置信区间不可用，保留原配置。", candidate

    guard_metric = link.get("guardMetric")
    guard_limit = link.get("maximumGuardRegressionRatio")
    guard_uncertain = False
    if isinstance(guard_metric, str) and isinstance(guard_limit, (int, float)):
        guard = _objective_by_metric(candidate, guard_metric)
        if guard is None or guard.get("status") != "available":
            return "inconclusive", "保护指标证据不足，保留原配置。", candidate
        guard_lower = guard.get("lower")
        guard_upper = guard.get("upper")
        if not isinstance(guard_lower, (int, float)) or not isinstance(
            guard_upper, (int, float)
        ):
            return "inconclusive", "保护指标置信区间不可用，保留原配置。", candidate
        guard_floor = -float(guard_limit)
        if guard_upper < guard_floor:
            return "rolled_back", "保护指标确认越界，保留原配置。", candidate
        guard_uncertain = guard_lower < guard_floor

    if lower >= threshold and not guard_uncertain:
        return "accepted", "优化收益与保护指标均通过，建议保留候选配置。", candidate
    if upper < threshold:
        return "rolled_back", "优化收益未达到最小有效差异，保留原配置。", candidate
    return "inconclusive", "置信区间跨过决策门槛，保留原配置并建议增加重复。", candidate


def post_optimization_view(
    session: Session, experiment: ExperimentRecord
) -> dict[str, Any]:
    spec = ExperimentSpec.model_validate(experiment.spec_json)
    link = _linked_state(experiment)
    if link is None:
        if ExperimentStatus(experiment.status) != ExperimentStatus.COMPLETED:
            return {
                "eligible": False,
                "status": "unavailable",
                "reason": "Benchmark 完成后才能分析并优化复测。",
            }
        if spec.mode != ExperimentMode.OPTIMIZATION:
            return {
                "eligible": False,
                "status": "unavailable",
                "reason": "当前选型研究只比较目标，没有声明可自动执行的安全动作。",
            }
        benchmark = _benchmark(session, spec)
        try:
            action, current, _analysis = _recommend_action(
                session, experiment, spec, benchmark
            )
        except SchedulerError as error:
            return {
                "eligible": False,
                "status": "unavailable",
                "reason": str(error),
            }
        return {
            "eligible": True,
            "status": "ready",
            "reason": "已从 Benchmark 的低风险动作白名单中找到一个未测试候选。",
            "action": {
                **action.model_dump(mode="json", by_alias=True),
                "before": current[action.parameter],
                "after": action.value,
            },
            "baselineParameters": current,
        }

    child = session.get(ExperimentRecord, str(link.get("followUpExperimentId")))
    if child is None:
        return {
            "eligible": False,
            "status": "failed",
            "reason": "关联的优化复测实验不存在。",
            "action": link.get("action"),
        }
    child_status = ExperimentStatus(child.status)
    if child_status in {
        ExperimentStatus.DRAFT,
        ExperimentStatus.QUEUED,
        ExperimentStatus.RUNNING,
        ExperimentStatus.PAUSED,
    }:
        status = "retesting"
        reason = "优化候选已经生成，正在使用同一 Benchmark 复测。"
        candidate = None
    elif child_status == ExperimentStatus.COMPLETED:
        status, reason, candidate = _completed_decision(session, child, link)
    else:
        status = "rolled_back"
        reason = "优化复测未成功完成，保留原配置。"
        candidate = None
    return {
        "eligible": False,
        "status": status,
        "reason": reason,
        "action": link.get("action"),
        "baselineParameters": link.get("baselineParameters"),
        "candidateParameters": candidate.get("parameters") if candidate else None,
        "followUpExperiment": experiment_view(session, child),
    }


def start_post_optimization(
    session: Session, experiment: ExperimentRecord
) -> dict[str, Any]:
    existing = _linked_state(experiment)
    if existing is not None:
        return post_optimization_view(session, experiment)
    if ExperimentStatus(experiment.status) != ExperimentStatus.COMPLETED:
        raise SchedulerError("Benchmark must complete before post-Benchmark optimization")
    source_spec = ExperimentSpec.model_validate(experiment.spec_json)
    if source_spec.mode != ExperimentMode.OPTIMIZATION:
        raise SchedulerError("selection studies do not declare executable optimization actions")
    benchmark = _benchmark(session, source_spec)
    action, baseline_parameters, _analysis = _recommend_action(
        session, experiment, source_spec, benchmark
    )
    follow_spec = _follow_up_spec(source_spec, baseline_parameters, action)
    child = create_experiment(
        session,
        ExperimentCreate(
            name=f"{experiment.name} · 优化复测",
            description=f"由 {experiment.id} 的完成结果触发：{action.label}",
            project_id=experiment.project_id,
            spec=follow_spec,
        ),
    )
    child.optimizer_state_json = {
        **child.optimizer_state_json,
        "post_optimization_source": {
            "experimentId": experiment.id,
            "actionId": action.id,
        },
    }
    start_experiment(session, child)
    link = {
        "followUpExperimentId": child.id,
        "createdAt": utc_now().isoformat(),
        "primaryMetric": source_spec.objectives[0].metric,
        "minimumImprovementRatio": action.minimum_improvement_ratio,
        "guardMetric": action.guard_metric,
        "maximumGuardRegressionRatio": action.maximum_guard_regression_ratio,
        "baselineParameters": baseline_parameters,
        "action": {
            **action.model_dump(mode="json", by_alias=True),
            "before": baseline_parameters[action.parameter],
            "after": action.value,
        },
    }
    experiment.optimizer_state_json = {
        **experiment.optimizer_state_json,
        "post_optimization": link,
    }
    experiment.revision += 1
    experiment.updated_at = utc_now()
    append_event(
        session,
        experiment_id=experiment.id,
        event_type="experiment.post_optimization.started",
        entity_type="experiment",
        entity_id=experiment.id,
        idempotency_key=f"experiment.post_optimization.started:{experiment.id}",
        payload={
            "follow_up_experiment_id": child.id,
            "action_id": action.id,
            "parameter": action.parameter,
            "before": baseline_parameters[action.parameter],
            "after": action.value,
        },
    )
    session.flush()
    return post_optimization_view(session, experiment)
