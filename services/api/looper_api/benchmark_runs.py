from __future__ import annotations

from typing import Any

from looper_core.contracts import (
    BenchmarkInputBinding,
    BudgetSpec,
    Direction,
    ExperimentalDesign,
    ExperimentCreate,
    ExperimentSpec,
    ObjectiveSpec,
    OptimizerSpec,
    SearchParameter,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from looper_api.models import BenchmarkRecord
from looper_api.scheduler import SchedulerError, create_experiment, start_experiment


class BenchmarkSmokeRunRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    target_id: str = Field(default="local", alias="targetId", min_length=1, max_length=100)
    workload_id: str | None = Field(default=None, alias="workloadId", max_length=120)
    parameters: dict[str, Any] = Field(default_factory=dict)
    input_bindings: dict[str, BenchmarkInputBinding] = Field(
        default_factory=dict, alias="inputBindings"
    )


def create_benchmark_smoke_run(
    session: Session,
    benchmark_id: str,
    version: str,
    request: BenchmarkSmokeRunRequest,
) -> Any:
    benchmark = session.scalar(
        select(BenchmarkRecord).where(
            BenchmarkRecord.benchmark_id == benchmark_id,
            BenchmarkRecord.version == version,
        )
    )
    if benchmark is None:
        raise SchedulerError("benchmark version is not installed")
    manifest_spec = benchmark.manifest_json["spec"]
    execution_status = manifest_spec.get("x-extensions", {}).get(
        "executionStatus", "executable"
    )
    if execution_status != "executable":
        raise SchedulerError("only executable benchmark packages can run smoke tests")
    declarations = manifest_spec["parameters"]
    unknown = sorted(set(request.parameters) - set(declarations))
    if unknown:
        raise SchedulerError(f"unknown benchmark parameters: {unknown}")
    search_space: dict[str, SearchParameter] = {}
    baseline: dict[str, Any] = {}
    for name, declaration in declarations.items():
        parameter = SearchParameter.model_validate(
            {key: value for key, value in declaration.items() if key != "description"}
        )
        search_space[name] = parameter
        value = request.parameters.get(name, parameter.default)
        if value is None:
            raise SchedulerError(
                f"smoke test parameter {name!r} requires a default or explicit value"
            )
        baseline[name] = value
    workloads = {item["id"]: item for item in manifest_spec["workloads"]}
    workload_id = request.workload_id or next(iter(workloads))
    if workload_id not in workloads:
        raise SchedulerError("smoke test workload is not declared")
    scenario = manifest_spec.get("scenario") or {}
    preferred_metric = scenario.get("primary_metric")
    metrics = manifest_spec["metrics"]
    objective_name = preferred_metric or next(
        (
            name
            for name, declaration in metrics.items()
            if declaration["direction"] in {"minimize", "maximize"}
        ),
        None,
    )
    if objective_name is None:
        raise SchedulerError("smoke test requires at least one directed metric")
    metric = metrics[objective_name]
    experiment = create_experiment(
        session,
        ExperimentCreate(
            name=f"Smoke · {benchmark.name} · {workload_id}",
            description="Configuration-driven benchmark package smoke test.",
            spec=ExperimentSpec(
                benchmark_id=benchmark.benchmark_id,
                benchmark_version=benchmark.version,
                target_ids=[request.target_id],
                workload_ids=[workload_id],
                input_bindings=request.input_bindings,
                baseline_parameters=baseline,
                search_space=search_space,
                objectives=[
                    ObjectiveSpec(
                        metric=objective_name,
                        unit=str(metric["unit"]),
                        direction=Direction(metric["direction"]),
                        minimum_samples=1,
                    )
                ],
                design=ExperimentalDesign(
                    warmup_runs=0,
                    min_repeats=1,
                    max_repeats=1,
                    max_retries=0,
                    bootstrap_resamples=100,
                    tail_min_samples=20,
                ),
                budget=BudgetSpec(
                    max_candidates=1,
                    max_attempts=1,
                    wall_time_seconds=600,
                ),
                optimizer=OptimizerSpec(type="random", seed=20260822),
            ),
        ),
    )
    return start_experiment(session, experiment)
