from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from looper_api import capacity
from looper_api.capacity import (
    BuildEvidence,
    BuildPlan,
    CapacityDraft,
    ScenarioPlan,
    ScenarioStep,
    TargetPlan,
)
from looper_api.capacity_evidence import (
    CAPACITY_METRIC_UNIT,
    CapacityEvidenceError,
    build_capacity_study_evidence,
)
from looper_api.config import Settings
from looper_api.models import (
    BenchmarkRecord,
    CandidateRecord,
    CapacityStudyRecord,
    EvaluationRecord,
    ExperimentRecord,
    SourceDiscoveryRecord,
    TargetRecord,
)
from looper_core.canonical import canonical_digest, utc_now
from looper_core.cas import FileSystemCAS
from looper_core.state import CandidateStatus, ExperimentStatus


def _target(target_id: str) -> TargetRecord:
    now = utc_now()
    snapshot = {
        "provider": "alibaba",
        "capabilities": ["python", "local-process", "alibaba-ecs"],
        "fingerprint": {"instance_type": "ecs.g8i.large", "os": "linux"},
    }
    return TargetRecord(
        id=target_id,
        name=target_id,
        provider="alibaba",
        status="available",
        capabilities_json=list(snapshot["capabilities"]),
        inventory_json={"instance_type": "ecs.g8i.large"},
        fingerprint_json=dict(snapshot["fingerprint"]),
        snapshot_digest=canonical_digest(snapshot),
        runnable=True,
        lifecycle_status="active",
        created_at=now,
        updated_at=now,
    )


def _draft(target_ids: list[str]) -> CapacityDraft:
    return CapacityDraft(
        build=BuildPlan(
            dockerfile="FROM python:3.12-slim\nCOPY . /app\n",
            compose="services:\n  app:\n    build: .\n",
            startCommand="python app.py",
            healthPath="/health",
            servicePort=8000,
            approved=True,
            evidence=[BuildEvidence(file="app.py", startLine=1, endLine=2)],
        ),
        scenario=ScenarioPlan(
            steps=[
                ScenarioStep(
                    id="read-order",
                    interfaceId="read-order",
                    label="read order",
                    method="GET",
                    path="/orders/1",
                )
            ]
        ),
        targets=TargetPlan(
            sutIds=target_ids,
            internalLoadGeneratorId="loadgen",
            externalLoadGeneratorId="loadgen",
            internalBaseUrls={target_id: f"http://{target_id}:8000" for target_id in target_ids},
            externalBaseUrls={
                target_id: f"https://{target_id}.example" for target_id in target_ids
            },
        ),
    )


def _completed_study(session: object) -> CapacityStudyRecord:
    now = utc_now()
    source = SourceDiscoveryRecord(
        id="discovery_capacity_evidence",
        archive_name="orders.zip",
        source_digest=canonical_digest({"source": "orders"}),
        status="completed",
        provider="deepseek",
        model="test",
        file_manifest_json=[{"path": "app.py", "bytes": 20, "lines": 2}],
        excluded_files_json=[],
        contract_json={"apiVersion": "test", "spec": {"interfaces": []}},
        trace_json=[],
        created_at=now,
        completed_at=now,
    )
    target_ids = ["sut-primary", "sut-control"]
    targets = [_target(target_id) for target_id in [*target_ids, "loadgen"]]
    session.add_all([source, *targets])
    session.flush()

    draft = _draft(target_ids)
    study = CapacityStudyRecord(
        id="capacity_evidence_test",
        discovery_id=source.id,
        name="capacity evidence",
        status="running",
        revision=3,
        current_step=4,
        draft_json=draft.model_dump(mode="json", by_alias=True),
        preflight_json={"status": "passed"},
        execution_json={
            "activeTargetIds": target_ids,
            "runs": [{"network": "internal", "experimentId": "exp_capacity_evidence"}],
        },
        report_json=None,
        created_at=now,
        updated_at=now,
        started_at=now,
    )
    session.add(study)
    session.flush()

    manifest = capacity._capacity_manifest(study, Settings(data_dir=Path(".looper")))
    manifest_digest = canonical_digest(manifest)
    metadata = manifest["metadata"]
    benchmark = BenchmarkRecord(
        key=f"{metadata['id']}@{metadata['version']}",
        benchmark_id=metadata["id"],
        version=metadata["version"],
        name=metadata["name"],
        description=metadata["description"],
        license=metadata["license"],
        manifest_digest=manifest_digest,
        manifest_json=manifest,
        trusted=True,
        installed_at=now,
    )
    session.add(benchmark)
    session.flush()

    request = capacity._network_experiment_request(session, study, benchmark, "internal")
    spec = request.spec
    experiment = ExperimentRecord(
        id="exp_capacity_evidence",
        project_id="default",
        name=request.name,
        description=request.description,
        status=ExperimentStatus.COMPLETED,
        spec_json=spec.model_dump(mode="json"),
        spec_digest=canonical_digest(spec.model_dump(mode="json")),
        revision=1,
        optimizer_state_json={},
        created_at=now,
        updated_at=now,
        started_at=now,
        finished_at=now,
    )
    candidate = CandidateRecord(
        id="candidate_capacity_evidence",
        experiment_id=experiment.id,
        sequence=0,
        role="scenario",
        parameters_json={},
        config_digest=canonical_digest({}),
        status=CandidateStatus.FEASIBLE,
        created_at=now,
        completed_at=now,
    )
    session.add(experiment)
    session.flush()
    session.add(candidate)
    session.flush()

    primary = session.get(TargetRecord, "sut-primary")
    assert primary is not None
    session.add(
        EvaluationRecord(
            id="evaluation_capacity_evidence",
            experiment_id=experiment.id,
            candidate_id=candidate.id,
            workload_id="business-iteration",
            target_id=primary.id,
            target_snapshot_digest=primary.snapshot_digest,
            target_snapshot_json={
                "snapshotDigest": primary.snapshot_digest,
                "provider": primary.provider,
                "capabilities": primary.capabilities_json,
                "fingerprint": primary.fingerprint_json,
                "inventory": primary.inventory_json,
            },
            status=CandidateStatus.FEASIBLE,
            created_at=now,
            completed_at=now,
        )
    )
    study.report_json = {
        "generatedAt": now.isoformat(),
        "capacityUnit": "successful business iterations/second",
        "confidenceLevel": spec.design.confidence_level,
        "networks": [
            {
                "network": "internal",
                "experimentId": experiment.id,
                "status": "resolved",
                "terminationReason": "bracket-resolved",
                "targets": [
                    {
                        "target_id": "sut-primary",
                        "frontiers": {
                            "unrelated-workload": {
                                "status": "resolved",
                                "confirmed_pass": 9999,
                                "confirmed_fail": 10000,
                            },
                            "business-iteration": {
                                "status": "resolved",
                                "confirmed_pass": 100,
                                "confirmed_fail": 120,
                            },
                        },
                    },
                    {
                        "target_id": "sut-control",
                        "frontiers": {
                            "business-iteration": {
                                "status": "resolved",
                                "confirmed_pass": 80,
                                "confirmed_fail": 90,
                            }
                        },
                    },
                ],
                "comparisons": [],
                "trajectory": [],
                "evidence": {},
            }
        ],
        "decision": "resolved",
    }
    study.status = "completed"
    study.completed_at = now
    study.updated_at = now
    session.flush()
    return study


def test_completed_study_is_normalized_into_replayable_cas_evidence(
    db_session: object, tmp_path: object
) -> None:
    study = _completed_study(db_session)
    cas = FileSystemCAS(tmp_path / "cas")

    first = build_capacity_study_evidence(
        db_session, study, cas, target_id="sut-primary", network="internal"
    )
    second = build_capacity_study_evidence(
        db_session, study, cas, target_id="sut-primary", network="internal"
    )

    assert first.evidence.frontier.confirmed_pass == 100
    assert first.evidence.frontier.confirmed_fail == 120
    assert first.evidence.control_frontiers["sut-control"].confirmed_pass == 80
    assert first.evidence.identity["capacity_unit"] == CAPACITY_METRIC_UNIT
    assert first.evidence.identity["source_digest"].startswith("sha256:")
    assert first.evidence.context_digest.startswith("sha256:")
    assert len(first.manifest.raw_artifacts) == 3
    assert len(first.manifest.normalized_artifacts) == 1
    assert first.manifest.evidence_id == second.manifest.evidence_id
    assert first.manifest_artifact.digest == second.manifest_artifact.digest
    for artifact in (
        first.report_artifact,
        first.study_contract_artifact,
        first.experiment_contract_artifact,
        first.normalized_artifact,
        first.manifest_artifact,
    ):
        assert cas.verify(artifact.digest, artifact.size).path.is_file()
    assert not db_session.new
    assert not db_session.dirty


def test_incomplete_study_fails_closed(db_session: object, tmp_path: object) -> None:
    study = _completed_study(db_session)
    study.status = "running"

    with pytest.raises(CapacityEvidenceError) as caught:
        build_capacity_study_evidence(
            db_session,
            study,
            FileSystemCAS(tmp_path / "cas"),
            target_id="sut-primary",
            network="internal",
        )

    assert caught.value.issue.code == "baseline_incomplete"
    assert caught.value.issue.recoverable is True


def test_missing_explicit_business_workload_is_not_replaced_by_first_frontier(
    db_session: object, tmp_path: object
) -> None:
    study = _completed_study(db_session)
    target = study.report_json["networks"][0]["targets"][0]
    del target["frontiers"]["business-iteration"]

    with pytest.raises(CapacityEvidenceError) as caught:
        build_capacity_study_evidence(
            db_session,
            study,
            FileSystemCAS(tmp_path / "cas"),
            target_id="sut-primary",
            network="internal",
        )

    assert caught.value.issue.code == "capacity_frontier_missing"
    assert caught.value.issue.details["available_workloads"] == ["unrelated-workload"]


def test_unresolved_frontier_reports_recoverable_evidence_gap(
    db_session: object, tmp_path: object
) -> None:
    study = _completed_study(db_session)
    frontier = study.report_json["networks"][0]["targets"][0]["frontiers"][
        "business-iteration"
    ]
    frontier["status"] = "unresolved"
    frontier["confirmed_fail"] = None

    with pytest.raises(CapacityEvidenceError) as caught:
        build_capacity_study_evidence(
            db_session,
            study,
            FileSystemCAS(tmp_path / "cas"),
            target_id="sut-primary",
            network="internal",
        )

    assert caught.value.issue.code == "capacity_frontier_unresolved"
    assert caught.value.issue.recoverable is True


def test_input_source_digest_mismatch_fails_before_artifact_write(
    db_session: object, tmp_path: object
) -> None:
    study = _completed_study(db_session)
    experiment = db_session.get(ExperimentRecord, "exp_capacity_evidence")
    assert experiment is not None
    payload = deepcopy(experiment.spec_json)
    metadata = payload["input_bindings"]["capacity-config"]["metadata"]
    metadata["sourceDigest"] = canonical_digest({"source": "different"})
    payload["input_bindings"]["capacity-config"]["digest"] = canonical_digest(metadata)
    experiment.spec_json = payload

    cas = FileSystemCAS(tmp_path / "cas")
    with pytest.raises(CapacityEvidenceError) as caught:
        build_capacity_study_evidence(
            db_session, study, cas, target_id="sut-primary", network="internal"
        )

    assert caught.value.issue.code == "capacity_input_identity_mismatch"
    assert not list((tmp_path / "cas" / "sha256").rglob("*"))


def test_control_frontier_must_also_be_resolved(db_session: object, tmp_path: object) -> None:
    study = _completed_study(db_session)
    control = study.report_json["networks"][0]["targets"][1]["frontiers"][
        "business-iteration"
    ]
    control["status"] = "unresolved"

    with pytest.raises(CapacityEvidenceError) as caught:
        build_capacity_study_evidence(
            db_session,
            study,
            FileSystemCAS(tmp_path / "cas"),
            target_id="sut-primary",
            network="internal",
        )

    assert caught.value.issue.code == "capacity_frontier_unresolved"
    assert caught.value.issue.details["target_id"] == "sut-control"


def test_environment_snapshot_digest_must_match_embedded_snapshot(
    db_session: object, tmp_path: object
) -> None:
    study = _completed_study(db_session)
    evaluation = db_session.get(EvaluationRecord, "evaluation_capacity_evidence")
    assert evaluation is not None
    evaluation.target_snapshot_json = {
        **evaluation.target_snapshot_json,
        "snapshotDigest": canonical_digest({"target": "different"}),
    }

    with pytest.raises(CapacityEvidenceError) as caught:
        build_capacity_study_evidence(
            db_session,
            study,
            FileSystemCAS(tmp_path / "cas"),
            target_id="sut-primary",
            network="internal",
        )

    assert caught.value.issue.code == "capacity_environment_digest_mismatch"


def test_report_confidence_must_match_experiment_contract(
    db_session: object, tmp_path: object
) -> None:
    study = _completed_study(db_session)
    study.report_json["confidenceLevel"] = 0.9

    with pytest.raises(CapacityEvidenceError) as caught:
        build_capacity_study_evidence(
            db_session,
            study,
            FileSystemCAS(tmp_path / "cas"),
            target_id="sut-primary",
            network="internal",
        )

    assert caught.value.issue.code == "capacity_confidence_mismatch"
