from __future__ import annotations

from pathlib import Path

import pytest
from looper_api.capacity import (
    BuildEvidence,
    BuildPlan,
    CapacityDraft,
    ScenarioPlan,
    ScenarioStep,
    TargetPlan,
)
from looper_api.capacity_candidate import (
    CapacityRecordStudyDriver,
    build_capacity_candidate_clone,
    evaluate_capacity_with_controls,
)
from looper_api.capacity_evidence import (
    CAPACITY_EVIDENCE_SCHEMA,
    CapacityStudyEvidence,
    ResolvedCapacityFrontier,
)
from looper_api.config import Settings
from looper_api.models import Base, CapacityStudyRecord, SourceDiscoveryRecord
from looper_api.system_optimization import CapacityTaskStatus
from looper_core.canonical import canonical_digest, utc_now
from looper_core.system_opt.hypothesis import hypothesis_context_digest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def _digest(seed: str) -> str:
    return canonical_digest({"seed": seed})


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
            sutIds=[*target_ids, "excluded-target"],
            internalLoadGeneratorId="loadgen-internal",
            externalLoadGeneratorId="loadgen-external",
            internalBaseUrls={
                **{target: f"http://{target}:8000" for target in target_ids},
                "excluded-target": "http://excluded:8000",
            },
            externalBaseUrls={
                **{target: f"https://{target}.example" for target in target_ids},
                "excluded-target": "https://excluded.example",
            },
        ),
    )


def _baseline(session: Session) -> CapacityStudyRecord:
    now = utc_now()
    source = SourceDiscoveryRecord(
        id="discovery-clone",
        archive_name="orders.zip",
        source_digest=_digest("source"),
        status="completed",
        provider="deepseek",
        model="test",
        file_manifest_json=[],
        excluded_files_json=[],
        contract_json={"spec": {"interfaces": [{"id": "read-order"}]}},
        trace_json=[],
        error_code=None,
        error_message=None,
        archive_retained_until=now,
        archive_deleted_at=None,
        archive_delete_reason=None,
        created_at=now,
        completed_at=now,
    )
    session.add(source)
    session.flush()
    active = ["target-a", "target-control"]
    draft = _draft(active)
    baseline = CapacityStudyRecord(
        id="capacity-baseline",
        discovery_id=source.id,
        name="baseline",
        status="completed",
        revision=4,
        current_step=4,
        draft_json=draft.model_dump(mode="json", by_alias=True),
        preflight_json={"draftRevision": 4},
        execution_json={
            "activeTargetIds": active,
            "buildValidations": [{"attempt": 1, "status": "passed"}],
            "runs": [],
        },
        report_json={"capacityUnit": "successful business iterations/second"},
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
        started_at=now,
        completed_at=now,
    )
    session.add(baseline)
    session.flush()
    return baseline


def test_candidate_clone_preserves_measurement_contract_and_all_active_servers() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        baseline = _baseline(session)
        candidate, clone = build_capacity_candidate_clone(
            session,
            baseline,
            candidate_capacity_study_id="capacity-candidate",
            target_id="target-a",
            network="internal",
            idempotency_key="optimization:1",
        )

        before = CapacityDraft.model_validate(baseline.draft_json)
        assert candidate.build == before.build
        assert candidate.scenario == before.scenario
        assert candidate.slo == before.slo
        assert candidate.budget == before.budget
        assert candidate.targets.sut_ids == ["target-a", "target-control"]
        assert set(candidate.targets.internal_base_urls) == {
            "target-a",
            "target-control",
        }
        assert clone.active_target_ids == ["target-a", "target-control"]
        assert clone.tuned_target_id == "target-a"
        assert clone.source_digest == _digest("source")


def test_capacity_driver_submission_is_deterministic_and_observed_without_callbacks(
    tmp_path: Path,
) -> None:
    del tmp_path
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        _baseline(session)
        session.commit()

    def preflight(_session, record, _settings):
        result = {
            "draftRevision": record.revision,
            "failedSutIds": [],
            "generatorFailures": [],
        }
        record.preflight_json = result
        return result

    def start(_session, record, _request, _settings):
        record.status = "queued"
        record.execution_json = {
            "activeTargetIds": CapacityDraft.model_validate(
                record.draft_json
            ).targets.sut_ids,
            "runs": [],
        }
        return record

    driver = CapacityRecordStudyDriver(
        factory,
        Settings(_env_file=None),
        preflight=preflight,
        start=start,
    )
    first = driver.submit_candidate(
        baseline_capacity_study_id="capacity-baseline",
        target_id="target-a",
        network="internal",
        hypothesis_digest=_digest("hypothesis"),
        idempotency_key="system-optimization:study-a",
    )
    second = driver.submit_candidate(
        baseline_capacity_study_id="capacity-baseline",
        target_id="target-a",
        network="internal",
        hypothesis_digest=_digest("hypothesis"),
        idempotency_key="system-optimization:study-a",
    )

    assert first == second
    pending = driver.observe(first)
    assert pending.status == CapacityTaskStatus.PENDING
    with factory() as session:
        candidate = session.get(CapacityStudyRecord, first)
        assert candidate is not None
        assert candidate.execution_json["optimizationClone"][
            "baseline_capacity_study_id"
        ] == "capacity-baseline"
        candidate.status = "failed"
        candidate.error_code = "injected_capacity_failure"
        session.commit()
    failed = driver.observe(first)
    assert failed.status == CapacityTaskStatus.FAILED
    assert failed.error_code == "injected_capacity_failure"


def _evidence(
    *,
    report: str,
    target_pass: float,
    target_fail: float,
    controls: dict[str, tuple[float, float]],
) -> CapacityStudyEvidence:
    identity = {
        "source_digest": _digest("source"),
        "workload_digest": _digest("workload"),
        "slo_digest": _digest("slo"),
        "environment_digest": _digest("environment"),
        "network": "internal",
        "target_id": "target-a",
        "capacity_unit": "successful business iterations/second",
        "confidence_level": "0.95",
        "measurement_contract_digest": _digest("measurement"),
    }
    return CapacityStudyEvidence(
        schema_version=CAPACITY_EVIDENCE_SCHEMA,
        study_id=f"study-{report}",
        experiment_id=f"experiment-{report}",
        target_id="target-a",
        network="internal",
        workload_id="business-iteration",
        metric_id="committed_tps",
        report_digest=_digest(report),
        study_contract_digest=_digest(f"study-{report}"),
        experiment_contract_digest=_digest(f"experiment-{report}"),
        benchmark_manifest_digest=_digest("benchmark"),
        frontier=ResolvedCapacityFrontier(
            status="resolved",
            confirmed_pass=target_pass,
            confirmed_fail=target_fail,
        ),
        control_frontiers={
            target: ResolvedCapacityFrontier(
                status="resolved", confirmed_pass=interval[0], confirmed_fail=interval[1]
            )
            for target, interval in controls.items()
        },
        active_target_ids=["target-a", *controls],
        identity=identity,
        context_digest=hypothesis_context_digest(identity),
    )


def test_capacity_decision_accepts_only_with_stable_control_and_verified_rollback() -> None:
    result = evaluate_capacity_with_controls(
        hypothesis_digest=_digest("hypothesis"),
        baseline=_evidence(
            report="baseline",
            target_pass=90,
            target_fail=100,
            controls={"target-control": (80, 90)},
        ),
        candidate=_evidence(
            report="candidate",
            target_pass=120,
            target_fail=130,
            controls={"target-control": (82, 91)},
        ),
        minimum_effect=0.05,
        rollback_verified=True,
    )

    assert result.outcome == "accepted"
    assert result.decision["status"] == "accepted"
    assert result.decision["coreDecision"]["lower"] == pytest.approx(0.2)
    assert result.decision["controlDrift"]["controls"]["target-control"][
        "positiveWidthOverlap"
    ] is True


def test_control_drift_or_missing_control_never_auto_accepts() -> None:
    baseline = _evidence(
        report="baseline",
        target_pass=90,
        target_fail=100,
        controls={"target-control": (80, 90)},
    )
    drifted = evaluate_capacity_with_controls(
        hypothesis_digest=_digest("hypothesis"),
        baseline=baseline,
        candidate=_evidence(
            report="candidate-drift",
            target_pass=120,
            target_fail=130,
            controls={"target-control": (100, 110)},
        ),
        minimum_effect=0.05,
        rollback_verified=True,
    )
    assert drifted.outcome == "inconclusive"
    assert drifted.decision["driftedControlTargetIds"] == ["target-control"]

    provisional = evaluate_capacity_with_controls(
        hypothesis_digest=_digest("hypothesis"),
        baseline=_evidence(
            report="baseline-one",
            target_pass=90,
            target_fail=100,
            controls={},
        ),
        candidate=_evidence(
            report="candidate-one",
            target_pass=120,
            target_fail=130,
            controls={},
        ),
        minimum_effect=0.05,
        rollback_verified=True,
    )
    assert provisional.outcome == "inconclusive"
    assert provisional.decision["status"] == "provisional"
