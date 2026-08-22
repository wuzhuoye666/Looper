"""Stability objectives: spec contract, evaluator, Pareto integration (VGO P1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from looper_api.analysis_service import build_analysis_snapshot
from looper_api.models import (
    AttemptRecord,
    CandidateRecord,
    EvaluationRecord,
    ExperimentRecord,
    ObservationRecord,
)
from looper_api.scheduler import create_demo_request, create_experiment, start_experiment
from looper_core.canonical import new_id, utc_now
from looper_core.contracts import (
    ExperimentSpec,
    StabilityMetric,
    StabilityObjectiveSpec,
)
from looper_core.state import AttemptStatus
from looper_core.variability import evaluate_stability_objective
from pydantic import ValidationError

# --- Contract -----------------------------------------------------------------------


def test_hard_stability_objective_requires_a_limit() -> None:
    with pytest.raises(ValidationError, match="hard stability objectives require"):
        StabilityObjectiveSpec(
            id="cv", metric=StabilityMetric.CV, target_metric="throughput_mib_s"
        )


def test_soft_stability_objective_rejects_limits() -> None:
    with pytest.raises(ValidationError, match="soft stability objectives"):
        StabilityObjectiveSpec(
            id="cv",
            metric=StabilityMetric.CV,
            target_metric="throughput_mib_s",
            hard=False,
            limit=0.1,
        )


def test_spec_rejects_stability_targeting_undeclared_metric() -> None:
    payload = create_demo_request().spec.model_dump(mode="json")
    payload["stability_objectives"] = [
        {
            "id": "cv",
            "metric": "cv",
            "target_metric": "no_such_metric",
            "limit": 0.1,
        }
    ]
    with pytest.raises(ValidationError, match="targets undeclared metric"):
        ExperimentSpec.model_validate(payload)


def test_selection_mode_rejects_stability_objectives() -> None:
    payload = create_demo_request().spec.model_dump(mode="json")
    payload["mode"] = "selection"
    payload["stability_objectives"] = [
        {
            "id": "cv",
            "metric": "cv",
            "target_metric": "throughput_mib_s",
            "limit": 0.1,
        }
    ]
    with pytest.raises(ValidationError, match="only supported in optimization mode"):
        ExperimentSpec.model_validate(payload)


# --- Core evaluator -----------------------------------------------------------------


def test_cv_limit_flags_volatile_candidate() -> None:
    objective = StabilityObjectiveSpec(
        id="cv-cap", metric=StabilityMetric.CV, target_metric="throughput_mib_s", limit=0.10
    )
    volatile = evaluate_stability_objective(
        [240, 241, 242, 243, 130, 131], [], objective, direction="maximize"
    )
    assert volatile["status"] == "violated"
    assert volatile["passed"] is False
    assert volatile["value"] > 0.10
    tight = evaluate_stability_objective(
        [200, 201, 202, 203, 204, 205], [], objective, direction="maximize"
    )
    assert tight["status"] == "satisfied"
    assert tight["value"] < 0.10


def test_p99_not_worse_than_baseline_respects_direction() -> None:
    # MINIMIZE (latency): a slower tail than baseline violates tolerance 0.
    objective = StabilityObjectiveSpec(
        id="tail-floor",
        metric=StabilityMetric.P99,
        target_metric="latency_ms",
        baseline_tolerance=0.0,
    )
    violated = evaluate_stability_objective(
        [12.0] * 6, [11.0] * 6, objective, direction="minimize"
    )
    assert violated["status"] == "violated"
    assert violated["baseline_value"] == pytest.approx(11.0)
    # A 10% tolerance lets a 9% degradation through.
    tolerant = StabilityObjectiveSpec(
        id="tail-floor",
        metric=StabilityMetric.P99,
        target_metric="latency_ms",
        baseline_tolerance=0.10,
    )
    tolerated = evaluate_stability_objective(
        [12.0] * 6, [11.0] * 6, tolerant, direction="minimize"
    )
    assert tolerated["status"] == "satisfied"
    # MAXIMIZE (throughput): the p95 floor must not drop below baseline.
    floor = StabilityObjectiveSpec(
        id="p95-floor",
        metric=StabilityMetric.P95,
        target_metric="throughput_mib_s",
        baseline_tolerance=0.0,
    )
    dropped = evaluate_stability_objective(
        [190.0] * 6, [200.0] * 6, floor, direction="maximize"
    )
    assert dropped["status"] == "violated"
    raised = evaluate_stability_objective(
        [210.0] * 6, [200.0] * 6, floor, direction="maximize"
    )
    assert raised["status"] == "satisfied"


def test_absolute_limit_is_a_floor_for_maximize_metrics() -> None:
    objective = StabilityObjectiveSpec(
        id="p95-floor",
        metric=StabilityMetric.P95,
        target_metric="throughput_mib_s",
        limit=195.0,
    )
    assert (
        evaluate_stability_objective(
            [190.0] * 6, [], objective, direction="maximize"
        )["status"]
        == "violated"
    )
    assert (
        evaluate_stability_objective(
            [200.0] * 6, [], objective, direction="maximize"
        )["status"]
        == "satisfied"
    )


def test_stability_fails_closed_without_evidence() -> None:
    limited = StabilityObjectiveSpec(
        id="cv",
        metric=StabilityMetric.CV,
        target_metric="throughput_mib_s",
        limit=0.5,
        minimum_samples=5,
    )
    few = evaluate_stability_objective([1.0, 2.0], [], limited, direction="minimize")
    assert few["status"] == "insufficient_evidence"
    assert few["passed"] is False
    assert few["pareto_value"] is None
    # A baseline-relative constraint cannot pass when baseline evidence is weak.
    relative = StabilityObjectiveSpec(
        id="tail",
        metric=StabilityMetric.P99,
        target_metric="latency_ms",
        baseline_tolerance=0.0,
        minimum_samples=5,
    )
    weak_baseline = evaluate_stability_objective(
        [10.0] * 6, [10.0, 11.0], relative, direction="minimize"
    )
    assert weak_baseline["status"] == "insufficient_evidence"
    assert weak_baseline["passed"] is False


# --- Service layer ------------------------------------------------------------------


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


def _attempt(
    session, experiment_id: str, evaluation_id: str, repeat: int, queue: int
) -> AttemptRecord:
    record = AttemptRecord(
        id=new_id("att"),
        experiment_id=experiment_id,
        evaluation_id=evaluation_id,
        selection_load_point_id=None,
        repeat_index=repeat,
        retry_index=0,
        queue_sequence=queue,
        status=AttemptStatus.SUCCEEDED,
        fencing_token=0,
        idempotency_key=new_id("idem"),
        envelope_json={
            "schemaVersion": "v1alpha1",
            "target": {"fingerprint": {"hostname": "demo-host-1"}},
        },
        created_at=datetime.now(UTC),
        started_at=datetime.now(UTC) - timedelta(days=repeat),
        completed_at=datetime.now(UTC),
    )
    session.add(record)
    return record


def _prepare_experiment(db_session, stability_objectives: list[StabilityObjectiveSpec]):
    """Baseline (tight) + stable candidate (tight, higher median) + spiky
    candidate (higher median, huge CV). All ratio observations tie so the
    compression-ratio dimension never decides dominance."""

    experiment = create_experiment(db_session, create_demo_request())
    start_experiment(db_session, experiment)
    db_session.flush()
    payload = dict(experiment.spec_json)
    payload["stability_objectives"] = [
        item.model_dump(mode="json") for item in stability_objectives
    ]
    experiment.spec_json = payload
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
    template = evaluation_by_candidate[baseline.id]
    spiky = CandidateRecord(
        id=new_id("cand"),
        experiment_id=experiment.id,
        sequence=max(item.sequence for item in candidates) + 1,
        role="candidate",
        parameters_json={"compression_level": 9, "chunk_size": 65536},
        config_digest="sha256:" + "9" * 64,
        status="completed",
        created_at=utc_now(),
    )
    db_session.add(spiky)
    spiky_evaluation = EvaluationRecord(
        id=new_id("eval"),
        experiment_id=experiment.id,
        candidate_id=spiky.id,
        workload_id=template.workload_id,
        target_id=template.target_id,
        target_snapshot_digest=template.target_snapshot_digest,
        target_snapshot_json=template.target_snapshot_json,
        status="completed",
        created_at=utc_now(),
    )
    db_session.add(spiky_evaluation)
    db_session.flush()

    def series(kind: str, count: int) -> list[float]:
        if kind == "baseline":
            return [200.0 + index * 0.5 for index in range(count)]
        if kind == "stable":
            return [210.0 + index * 0.5 for index in range(count)]
        # spiky: best median of all three, but a third of the runs collapse.
        half = count // 2 + 1
        high = [240.0 + index for index in range(half)]
        low = [130.0 + index for index in range(count - half)]
        return high + low

    queue = 9000
    for candidate, evaluation_id, kind in (
        (baseline, evaluation_by_candidate[baseline.id].id, "baseline"),
        (scheduled, evaluation_by_candidate[scheduled.id].id, "stable"),
        (spiky, spiky_evaluation.id, "spiky"),
    ):
        attempts = (
            db_session.query(AttemptRecord)
            .where(
                AttemptRecord.evaluation_id == evaluation_id,
                AttemptRecord.experiment_id == experiment.id,
            )
            .all()
        )
        if candidate.id == spiky.id:
            attempts = []
        attempts = list(attempts)
        for attempt in attempts:
            attempt.status = AttemptStatus.SUCCEEDED
            attempt.envelope_json = {
                "schemaVersion": "v1alpha1",
                "target": {"fingerprint": {"hostname": "demo-host-1"}},
            }
        while len(attempts) < 6:
            extra = _attempt(db_session, experiment.id, evaluation_id, len(attempts), queue)
            queue += 1
            attempts.append(extra)
        db_session.flush()
        values = series(kind, len(attempts))
        for attempt, value in zip(attempts, values, strict=True):
            _observation(db_session, attempt.id, "throughput_mib_s", value, "MiB/s")
            _observation(db_session, attempt.id, "compression_ratio", 0.01, "ratio")
            _observation(db_session, attempt.id, "roundtrip_ok", True, "boolean")
    db_session.flush()
    labels = {
        baseline.id: "baseline",
        scheduled.id: "stable",
        spiky.id: "spiky",
    }
    return experiment.id, labels


def test_hard_cv_limit_makes_unstable_candidate_infeasible(db_session) -> None:
    experiment_id, labels = _prepare_experiment(
        db_session,
        [
            StabilityObjectiveSpec(
                id="cv-cap",
                metric=StabilityMetric.CV,
                target_metric="throughput_mib_s",
                limit=0.10,
            )
        ],
    )
    result = build_analysis_snapshot(db_session, experiment_id, persist=False)
    by_role = {}
    for candidate in result["candidates"]:
        by_role[labels[candidate["id"]]] = candidate
    assert by_role["spiky"]["status"] == "infeasible"
    assert "stability:cv-cap(violated)" in by_role["spiky"]["reason"]
    assert by_role["spiky"]["feasible"] is False
    assert by_role["spiky"]["pareto_rank"] is None
    stability = by_role["spiky"]["stability"][0]
    assert stability["status"] == "violated"
    assert stability["value"] > 0.10
    assert by_role["stable"]["status"] == "feasible"
    assert by_role["baseline"]["status"] == "feasible"
    # Hard objective must not double as a Pareto dimension.
    assert all(
        "stability:cv-cap" not in point["objectives"] for point in result["pareto"]
    )


def test_soft_cv_objective_joins_pareto_ranking(db_session) -> None:
    experiment_id, labels = _prepare_experiment(
        db_session,
        [
            StabilityObjectiveSpec(
                id="cv-rank",
                metric=StabilityMetric.CV,
                target_metric="throughput_mib_s",
                hard=False,
            )
        ],
    )
    result = build_analysis_snapshot(db_session, experiment_id, persist=False)
    by_role = {
        labels[candidate["id"]]: candidate for candidate in result["candidates"]
    }
    for role in ("baseline", "stable", "spiky"):
        assert by_role[role]["status"] == "feasible"
    # The spiky candidate keeps the best median throughput...
    stability = {
        labels[point["candidate_id"]]: point["objectives"].get("stability:cv-rank")
        for point in result["pareto"]
    }
    assert stability["spiky"] > stability["stable"]
    # ...but its CV is so much worse that it no longer dominates the stable
    # candidate: both share the Pareto front instead of mean-only ranking.
    assert by_role["spiky"]["pareto_rank"] == 1
    assert by_role["stable"]["pareto_rank"] == 1

    # Control: without the stability objective the spiky candidate dominates.
    experiment = db_session.get(ExperimentRecord, experiment_id)
    payload = dict(experiment.spec_json)
    payload["stability_objectives"] = []
    experiment.spec_json = payload
    db_session.flush()
    control = build_analysis_snapshot(db_session, experiment_id, persist=False)
    by_role = {
        labels[candidate["id"]]: candidate for candidate in control["candidates"]
    }
    assert by_role["spiky"]["pareto_rank"] == 1
    assert by_role["stable"]["pareto_rank"] == 2
