from __future__ import annotations

from looper_core.cas import FileSystemCAS
from sqlalchemy.orm import object_session

from looper_api.capacity_candidate import CapacityRecordStudyDriver, RealCapacityStudyEvaluator
from looper_api.config import Settings
from looper_api.database import SessionLocal
from looper_api.restricted_alibaba_sysfs import build_restricted_alibaba_sysfs_backend
from looper_api.system_optimization_inputs import load_authorization_profile
from looper_api.system_optimization_worker import (
    ReconciliationCycleReport,
    ReconciliationResources,
    run_reconciliation_cycle,
)


def reconcile_system_optimization_studies(
    settings: Settings,
) -> ReconciliationCycleReport:
    cas = FileSystemCAS(settings.artifact_dir, max_bytes=settings.max_artifact_bytes)
    capacity_driver = CapacityRecordStudyDriver(SessionLocal, settings)
    evaluator = RealCapacityStudyEvaluator(SessionLocal, cas)

    def resources(record):
        profile = load_authorization_profile(
            record_session(record),
            cas,
            record.authorization_profile_digest,
            target_id=record.target_id,
        )
        backend = build_restricted_alibaba_sysfs_backend(
            record_session(record), record.target_id, settings
        )
        return ReconciliationResources(
            backend=backend,
            manifest=profile.manifest,
            capacity_driver=capacity_driver,
            evaluator=evaluator,
        )

    return run_reconciliation_cycle(SessionLocal, cas, resources)


def record_session(record):
    session = object_session(record)
    if session is None:
        raise RuntimeError("system optimization record is detached from its worker session")
    return session


__all__ = ["reconcile_system_optimization_studies"]
