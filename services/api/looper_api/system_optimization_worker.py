from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from looper_core.cas import FileSystemCAS
from looper_core.contracts import StrictModel
from looper_core.system_opt.config_manifest import ConfigManifest
from looper_core.system_opt.executor import ExecutorBackend
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from looper_api.system_optimization import (
    CapacityStudyDriver,
    ReconcileResult,
    StudyEvaluator,
    SystemOptimizationError,
    SystemOptimizationProblem,
    SystemOptimizationStatus,
    halt_system_optimization_study,
    reconcile_system_optimization_study,
)
from looper_api.system_optimization_models import SystemOptimizationStudyRecord


@dataclass(frozen=True, slots=True)
class ReconciliationResources:
    backend: ExecutorBackend
    manifest: ConfigManifest
    capacity_driver: CapacityStudyDriver
    evaluator: StudyEvaluator


class ReconciliationResourceProvider(Protocol):
    def __call__(
        self, record: SystemOptimizationStudyRecord
    ) -> ReconciliationResources: ...


class ReconciliationCycleReport(StrictModel):
    reconciled: list[ReconcileResult]
    stopped_study_id: str | None = None
    problem: SystemOptimizationProblem | None = None
    inspected_study_ids: list[str] = Field(default_factory=list)


def _unexpected_problem(error: Exception) -> SystemOptimizationProblem:
    return SystemOptimizationProblem(
        stage="evaluation",
        code="reconciliation_unhandled_error",
        message="reconciliation stopped on an unhandled implementation error",
        evidence_summary={"errorType": type(error).__name__},
        suggested_action="Inspect worker logs and the persisted evidence before retrying.",
    )


def run_reconciliation_cycle(
    session_factory: Callable[[], Session],
    cas: FileSystemCAS,
    resources_for: ReconciliationResourceProvider,
) -> ReconciliationCycleReport:
    active = {
        SystemOptimizationStatus.APPLYING.value,
        SystemOptimizationStatus.MEASURING.value,
        SystemOptimizationStatus.ROLLING_BACK.value,
        SystemOptimizationStatus.EVALUATING.value,
    }
    with session_factory() as discovery_session:
        study_ids = list(
            discovery_session.scalars(
                select(SystemOptimizationStudyRecord.id)
                .where(SystemOptimizationStudyRecord.status.in_(active))
                .order_by(SystemOptimizationStudyRecord.updated_at)
            )
        )
    reconciled: list[ReconcileResult] = []
    inspected: list[str] = []
    for study_id in study_ids:
        inspected.append(study_id)
        with session_factory() as session:
            record = session.get(SystemOptimizationStudyRecord, study_id)
            if record is None or record.status not in active:
                continue
            try:
                resources = resources_for(record)
                result = reconcile_system_optimization_study(
                    session,
                    cas,
                    record,
                    backend=resources.backend,
                    manifest=resources.manifest,
                    capacity_driver=resources.capacity_driver,
                    evaluator=resources.evaluator,
                )
                session.commit()
                reconciled.append(result)
            except Exception as error:
                session.rollback()
                problem = (
                    error.problem
                    if isinstance(error, SystemOptimizationError)
                    else _unexpected_problem(error)
                )
                current = session.get(SystemOptimizationStudyRecord, study_id)
                if current is not None:
                    halt_system_optimization_study(current, problem)
                    session.commit()
                return ReconciliationCycleReport(
                    reconciled=reconciled,
                    stopped_study_id=study_id,
                    problem=problem,
                    inspected_study_ids=inspected,
                )
    return ReconciliationCycleReport(
        reconciled=reconciled,
        inspected_study_ids=inspected,
    )


__all__ = [
    "ReconciliationCycleReport",
    "ReconciliationResourceProvider",
    "ReconciliationResources",
    "run_reconciliation_cycle",
]
