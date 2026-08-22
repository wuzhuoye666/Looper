from __future__ import annotations

from datetime import UTC, datetime

import pytest
from looper_api.models import (
    AttemptRecord,
    CandidateRecord,
    EvaluationRecord,
    ExperimentRecord,
    ObservationRecord,
)
from looper_api.post_optimization import post_optimization_view, start_post_optimization
from looper_api.scheduler import create_demo_request, create_experiment, start_experiment
from looper_core.canonical import new_id, utc_now
from looper_core.state import AttemptStatus, CandidateStatus, ExperimentStatus


def _observation(
    session: object,
    attempt_id: str,
    metric: str,
    value: float | bool,
    unit: str,
) -> None:
    session.add(  # type: ignore[attr-defined]
        ObservationRecord(
            id=new_id("obs"),
            attempt_id=attempt_id,
            metric=metric,
            value_number=float(value) if not isinstance(value, bool) else None,
            value_boolean=value if isinstance(value, bool) else None,
            unit=unit,
            phase="measurement",
            workload="medium",
            sample_index=None,
            sample_count=1,
            statistic="sample",
            attributes_json={},
            created_at=datetime.now(UTC),
        )
    )


def _complete_attempts(
    session: object,
    experiment_id: str,
    *,
    baseline_throughput: float,
    candidate_throughput: float | None = None,
    baseline_ratio: float = 0.10,
    candidate_ratio: float = 0.10,
) -> None:
    candidates = {
        item.id: item
        for item in session.query(CandidateRecord)  # type: ignore[attr-defined]
        .filter_by(experiment_id=experiment_id)
        .all()
    }
    evaluations = {
        item.id: item
        for item in session.query(EvaluationRecord)  # type: ignore[attr-defined]
        .filter_by(experiment_id=experiment_id)
        .all()
    }
    for attempt in (
        session.query(AttemptRecord)  # type: ignore[attr-defined]
        .filter_by(experiment_id=experiment_id)
        .all()
    ):
        evaluation = evaluations[attempt.evaluation_id]
        candidate = candidates[evaluation.candidate_id]
        is_baseline = candidate.role == "baseline"
        throughput = (
            baseline_throughput
            if is_baseline or candidate_throughput is None
            else candidate_throughput
        )
        ratio = baseline_ratio if is_baseline else candidate_ratio
        attempt.status = AttemptStatus.SUCCEEDED
        attempt.started_at = utc_now()
        attempt.completed_at = utc_now()
        _observation(session, attempt.id, "throughput_mib_s", throughput, "MiB/s")
        _observation(session, attempt.id, "compression_ratio", ratio, "ratio")
        _observation(session, attempt.id, "roundtrip_ok", True, "bool")
    for candidate in candidates.values():
        candidate.status = CandidateStatus.FEASIBLE
        candidate.completed_at = utc_now()
    for evaluation in evaluations.values():
        evaluation.status = CandidateStatus.FEASIBLE
        evaluation.completed_at = utc_now()
    session.flush()  # type: ignore[attr-defined]


def _completed_source(session: object) -> ExperimentRecord:
    request = create_demo_request("Finished Benchmark")
    request.spec.budget.max_candidates = 1
    request.spec.budget.max_attempts = 3
    experiment = create_experiment(session, request)  # type: ignore[arg-type]
    start_experiment(session, experiment)  # type: ignore[arg-type]
    _complete_attempts(session, experiment.id, baseline_throughput=100.0)
    experiment.status = ExperimentStatus.COMPLETED
    experiment.finished_at = utc_now()
    session.flush()  # type: ignore[attr-defined]
    return experiment


def test_completed_benchmark_exposes_low_risk_optimization_action(db_session: object) -> None:
    experiment = _completed_source(db_session)

    view = post_optimization_view(db_session, experiment)  # type: ignore[arg-type]

    assert view["eligible"] is True
    assert view["status"] == "ready"
    assert view["action"]["id"] == "larger-compression-chunks"
    assert view["action"]["parameter"] == "chunk_size"
    assert view["action"]["before"] == 16384
    assert view["action"]["after"] == 65536


def test_optimization_button_creates_one_linked_retest_experiment(db_session: object) -> None:
    experiment = _completed_source(db_session)

    started = start_post_optimization(db_session, experiment)  # type: ignore[arg-type]
    repeated = start_post_optimization(db_session, experiment)  # type: ignore[arg-type]

    assert started["status"] == "retesting"
    assert repeated["followUpExperiment"]["id"] == started["followUpExperiment"]["id"]
    child_id = started["followUpExperiment"]["id"]
    child = db_session.get(ExperimentRecord, child_id)  # type: ignore[attr-defined]
    assert child is not None
    assert child.status == ExperimentStatus.QUEUED
    candidates = (
        db_session.query(CandidateRecord)  # type: ignore[attr-defined]
        .filter_by(experiment_id=child_id)
        .order_by(CandidateRecord.sequence)
        .all()
    )
    assert [item.parameters_json for item in candidates] == [
        {"compression_level": 6, "chunk_size": 16384},
        {"compression_level": 6, "chunk_size": 65536},
    ]


@pytest.mark.parametrize(
    ("candidate_throughput", "candidate_ratio", "expected"),
    [(120.0, 0.10, "accepted"), (120.0, 0.20, "rolled_back")],
)
def test_retest_decision_keeps_only_safe_improvement(
    db_session: object,
    candidate_throughput: float,
    candidate_ratio: float,
    expected: str,
) -> None:
    source = _completed_source(db_session)
    started = start_post_optimization(db_session, source)  # type: ignore[arg-type]
    child_id = started["followUpExperiment"]["id"]
    child = db_session.get(ExperimentRecord, child_id)  # type: ignore[attr-defined]
    assert child is not None
    _complete_attempts(
        db_session,
        child_id,
        baseline_throughput=100.0,
        candidate_throughput=candidate_throughput,
        baseline_ratio=0.10,
        candidate_ratio=candidate_ratio,
    )
    child.status = ExperimentStatus.COMPLETED
    child.finished_at = utc_now()
    db_session.flush()  # type: ignore[attr-defined]

    view = post_optimization_view(db_session, source)  # type: ignore[arg-type]

    assert view["status"] == expected
    if expected == "accepted":
        assert view["candidateParameters"] == {
            "compression_level": 6,
            "chunk_size": 65536,
        }
    else:
        assert "保留原配置" in view["reason"]
