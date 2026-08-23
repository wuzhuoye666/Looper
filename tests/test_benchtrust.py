from __future__ import annotations

from datetime import UTC, datetime

from looper_api.analysis_service import build_analysis_snapshot
from looper_api.models import (
    AttemptRecord,
    CandidateRecord,
    EvaluationRecord,
    ObservationRecord,
)
from looper_api.scheduler import create_demo_request, create_experiment, start_experiment
from looper_api.serialization import analysis_view
from looper_core.canonical import new_id
from looper_core.state import AttemptStatus


def _observation(session, attempt_id: str, metric: str, value, unit: str) -> None:
    session.add(
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


def _prepare_demo(db_session) -> str:
    """Baseline plus one scheduled candidate, both objectives observed."""
    experiment = create_experiment(db_session, create_demo_request())
    start_experiment(db_session, experiment)
    db_session.flush()

    candidates = (
        db_session.query(CandidateRecord)
        .where(CandidateRecord.experiment_id == experiment.id)
        .order_by(CandidateRecord.sequence)
        .all()
    )
    baseline = next(item for item in candidates if item.role == "baseline")
    scheduled = next(item for item in candidates if item.role != "baseline")
    evaluations = (
        db_session.query(EvaluationRecord)
        .where(EvaluationRecord.experiment_id == experiment.id)
        .all()
    )
    evaluation_by_candidate = {item.candidate_id: item for item in evaluations}

    for candidate, throughput in ((baseline, 200.0), (scheduled, 220.0)):
        evaluation = evaluation_by_candidate[candidate.id]
        attempts = (
            db_session.query(AttemptRecord)
            .where(
                AttemptRecord.evaluation_id == evaluation.id,
                AttemptRecord.experiment_id == experiment.id,
            )
            .all()
        )
        for attempt in attempts:
            attempt.status = AttemptStatus.SUCCEEDED
            attempt.completed_at = datetime.now(UTC)
        for index, attempt in enumerate(attempts):
            _observation(db_session, attempt.id, "throughput_mib_s", throughput + index, "MiB/s")
            _observation(db_session, attempt.id, "compression_ratio", 0.01, "ratio")
            _observation(db_session, attempt.id, "roundtrip_ok", True, "boolean")
    db_session.flush()
    return experiment.id


def test_benchtrust_structure_is_complete_without_overall_score(db_session) -> None:
    experiment_id = _prepare_demo(db_session)
    result = build_analysis_snapshot(db_session, experiment_id, persist=False)

    benchtrust = result["benchtrust"]
    assert set(benchtrust) >= {
        "schemaVersion",
        "methodVersion",
        "status",
        "referenceValidityRate",
        "rankStability",
        "taskLeverage",
        "environmentSensitivity",
        "evidence",
        "limitations",
        "inputDigest",
        "policyDigest",
    }
    # No opaque composite trust score is produced.
    assert "overall_score" not in benchtrust
    assert "score" not in benchtrust

    for key in (
        "referenceValidityRate",
        "rankStability",
        "taskLeverage",
        "environmentSensitivity",
    ):
        assert benchtrust[key]["status"] in {
            "available",
            "partial",
            "insufficient_evidence",
            "unavailable",
        }

    # Each metric exposes its method and limitations.
    assert benchtrust["referenceValidityRate"]["method"]
    assert benchtrust["rankStability"]["axes"]
    assert benchtrust["environmentSensitivity"]["association_only"] is True


def test_benchtrust_task_leverage_uses_objective_weights(db_session) -> None:
    experiment_id = _prepare_demo(db_session)
    result = build_analysis_snapshot(db_session, experiment_id, persist=False)
    leverage = result["benchtrust"]["taskLeverage"]
    # Two objectives are declared; the demo has enough candidates to rank them.
    assert leverage["status"] in {"available", "partial", "insufficient_evidence"}
    assert leverage["aggregation_method"] in {"weighted-sum", None}


def test_analysis_view_preserves_legacy_flat_benchtrust(db_session) -> None:
    legacy_benchtrust = {
        "reference_validity_rate": 0.75,
        "rank_stability": {"mean_kendall_tau": 1.0},
        "task_leverage": {"dominant_task": "x"},
        "environment_sensitivity": {"eta_squared": 0.1},
    }
    result = {
        "experiment_id": "exp-legacy",
        "candidates": [],
        "pareto": [],
        "benchtrust": legacy_benchtrust,
        "evidence": {"attempt_count": 0, "observation_count": 0, "artifact_count": 0},
    }
    view = analysis_view(result)
    # analysis_view must not assume the new shape and must pass it through.
    assert view["benchtrust"] == legacy_benchtrust
    assert "pareto" in view


def test_benchtrust_single_environment_is_insufficient_not_zero(db_session) -> None:
    experiment_id = _prepare_demo(db_session)
    result = build_analysis_snapshot(db_session, experiment_id, persist=False)
    reference = result["benchtrust"]["referenceValidityRate"]
    # A single eligible environment reports its result but can never claim
    # cross-environment validity.
    assert reference["status"] == "insufficient_evidence"
    assert reference["eligible_environment_count"] == 1
    # rate is a faithful ratio, never a fabricated number from missing data.
    assert reference["rate"] == 1.0