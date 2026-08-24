from __future__ import annotations

import importlib.util
from pathlib import Path

from looper_api.models import (
    AnalysisSnapshotRecord,
    ArtifactLinkRecord,
    ArtifactRecord,
    AttemptRecord,
    CheckRecord,
    EventRecord,
    ExperimentRecord,
    ObservationRecord,
    SelectionLoadPointRecord,
    TargetRecord,
)
from looper_api.scheduler import create_demo_request, create_experiment, start_experiment
from looper_core.canonical import canonical_digest, new_id, utc_now
from sqlalchemy import func, select

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations/versions/e1a6b5c4d3f2_remove_local_workstation.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "remove_local_workstation_migration", _MIGRATION_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
_MIGRATION = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MIGRATION)
remove_local_workstation = _MIGRATION.remove_local_workstation


def _count(session, model, field, value) -> int:
    return int(session.scalar(select(func.count()).select_from(model).where(field == value)) or 0)


def test_remove_local_workstation_deletes_history_and_keeps_artifact(db_session) -> None:
    local_experiment = create_experiment(db_session, create_demo_request("local history"))
    start_experiment(db_session, local_experiment)
    attempt = db_session.scalar(
        select(AttemptRecord).where(AttemptRecord.experiment_id == local_experiment.id)
    )
    digest = "sha256:" + "a" * 64
    db_session.add(ArtifactRecord(digest=digest, size=1, verified=True, created_at=utc_now()))
    db_session.flush()
    db_session.add_all([
        ObservationRecord(
            id=new_id("obs"), attempt_id=attempt.id, metric="throughput_mib_s",
            value_number=1.0, value_boolean=None, unit="MiB/s", phase="measurement",
            workload="medium", sample_index=None, sample_count=1, statistic="median",
            timestamp_text=None, attributes_json={}, created_at=utc_now(),
        ),
        CheckRecord(
            id=new_id("chk"), attempt_id=attempt.id, check_id="migration-test",
            passed=True, scope="attempt", kind="correctness", message=None,
            details_json={}, created_at=utc_now(),
        ),
        ArtifactLinkRecord(
            id=new_id("alink"), attempt_id=attempt.id, digest=digest, role="raw",
            name="raw.txt", media_type="text/plain", producer="test", created_at=utc_now(),
        ),
        AnalysisSnapshotRecord(
            id=new_id("analysis"), experiment_id=local_experiment.id,
            policy_digest="sha256:" + "b" * 64, input_digest="sha256:" + "c" * 64,
            code_version="test", status="complete", result_json={}, created_at=utc_now(),
        ),
        SelectionLoadPointRecord(
            id=new_id("load"), experiment_id=local_experiment.id, workload_id="medium",
            sequence=1, offered_load=1, offered_load_key="1", origin="initial",
            required_repeats=1, status="pending", analysis_json={},
            analysis_input_digest=None, created_at=utc_now(), completed_at=None,
        ),
    ])

    spec_only_id = new_id("exp")
    spec = create_demo_request("draft only").spec.model_dump(mode="json")
    db_session.add(ExperimentRecord(
        id=spec_only_id, project_id="default", name="draft only", description="",
        status="draft", spec_json=spec, spec_digest=canonical_digest(spec), revision=1,
        optimizer_state_json={}, created_at=utc_now(), updated_at=utc_now(),
    ))

    remote = TargetRecord(
        id="remote-keep", name="Remote keep", provider="external", status="online",
        capabilities_json=["python", "local-process", "linux", "x86_64"],
        inventory_json={},
        fingerprint_json={
            "system": "Linux", "architecture": "x86_64",
            "logical_cpu_count": 8, "memory_gib": 16,
        },
        snapshot_digest="sha256:" + "d" * 64, runnable=True, lifecycle_status="active",
        created_at=utc_now(), updated_at=utc_now(),
    )
    db_session.add(remote)
    db_session.flush()
    unrelated_request = create_demo_request("unrelated")
    unrelated_request.spec.target_ids = [remote.id]
    unrelated = create_experiment(db_session, unrelated_request)
    db_session.flush()
    local_experiment_id = local_experiment.id
    remote_id = remote.id
    unrelated_id = unrelated.id

    remove_local_workstation(db_session.connection())
    db_session.expire_all()

    assert db_session.get(TargetRecord, "local") is None
    assert db_session.get(TargetRecord, remote_id) is not None
    assert db_session.get(ExperimentRecord, local_experiment_id) is None
    assert db_session.get(ExperimentRecord, spec_only_id) is None
    assert db_session.get(ExperimentRecord, unrelated_id) is not None
    assert db_session.get(ArtifactRecord, digest) is not None
    assert _count(db_session, AttemptRecord, AttemptRecord.experiment_id, local_experiment_id) == 0
    assert _count(db_session, EventRecord, EventRecord.experiment_id, local_experiment_id) == 0
