from __future__ import annotations

import pytest
from looper_api.analysis_service import build_analysis_snapshot
from looper_api.app import _normalize_create_request
from looper_api.models import (
    AttemptRecord,
    BenchmarkRecord,
    CheckRecord,
    EvaluationRecord,
    ObservationRecord,
    SelectionLoadPointRecord,
    TargetRecord,
)
from looper_api.scheduler import (
    SchedulerError,
    advance_experiment,
    create_experiment,
    start_experiment,
)
from looper_api.serialization import benchmark_view, experiment_view
from looper_core.canonical import canonical_digest, new_id, utc_now
from looper_core.contracts import ExperimentMode
from looper_core.state import AttemptStatus, ExperimentStatus
from sqlalchemy import select


def test_scenario_catalog_exposes_execution_boundary(db_session: object) -> None:
    session = db_session
    records = list(session.scalars(select(BenchmarkRecord).order_by(BenchmarkRecord.key)))
    views = {record.benchmark_id: benchmark_view(record) for record in records}
    assert set(views) >= {
        "looper.demo.compression",
        "benchbase.smallbank.postgres",
        "dcperf.mediawiki.closed-loop",
    }
    benchbase = views["benchbase.smallbank.postgres"]
    assert benchbase["category"] == "scenario"
    assert benchbase["executionStatus"] == "stage0-adapter-only"
    assert benchbase["runnable"] is False
    assert benchbase["primaryMetric"] == "committed_tps"


def test_selection_study_can_be_saved_but_stage0_adapter_cannot_start(
    db_session: object,
) -> None:
    session = db_session
    request = _normalize_create_request(
        {
            "mode": "selection",
            "name": "SmallBank SKU pilot",
            "benchmarkId": "benchbase.smallbank.postgres",
            "targetIds": ["local"],
            "config": {"repeats": 5, "seed": 77, "timeout": 86400},
        },
        session,
    )
    assert request.spec.mode == ExperimentMode.SELECTION
    assert request.spec.selection is not None
    assert request.spec.selection.target_bindings[0].target_id == "local"
    experiment = create_experiment(session, request)
    view = experiment_view(session, experiment, detail=True)
    assert experiment.status == ExperimentStatus.DRAFT
    assert view["config"]["mode"] == "selection"
    with pytest.raises(SchedulerError, match="stage0-adapter-only"):
        start_experiment(session, experiment)
    assert experiment.status == ExperimentStatus.DRAFT


def test_selection_analysis_pairs_time_blocks_by_target_variant(db_session: object) -> None:
    session = db_session
    local = session.get(TargetRecord, "local")
    assert local is not None
    for target_id in ["target-a", "target-b"]:
        capabilities = ["linux", "x86_64", "container", "benchbase", "postgresql"]
        snapshot = {
            "provider": "fixture",
            "capabilities": capabilities,
            "fingerprint": {"processor": target_id, "logical_cpu_count": 8},
        }
        session.add(
            TargetRecord(
                id=target_id,
                name=target_id.upper(),
                provider="fixture",
                status="available",
                capabilities_json=capabilities,
                inventory_json={"instance_type": target_id},
                fingerprint_json=snapshot["fingerprint"],
                snapshot_digest=canonical_digest(snapshot),
                runnable=True,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
    benchmark = session.scalar(
        select(BenchmarkRecord).where(
            BenchmarkRecord.benchmark_id == "benchbase.smallbank.postgres"
        )
    )
    assert benchmark is not None
    benchmark.manifest_json["spec"]["x-extensions"]["executionStatus"] = "executable"
    benchmark.manifest_json["spec"]["scenario"].pop("load_search", None)
    benchmark.manifest_json["spec"]["runtime"]["image"] = (
        "example.invalid/benchbase@sha256:" + "0" * 64
    )
    request = _normalize_create_request(
        {
            "mode": "selection",
            "name": "Paired target fixture",
            "benchmarkId": benchmark.benchmark_id,
            "targetIds": ["target-a", "target-b"],
            "targetBindings": [
                {"targetId": "target-a", "variantId": "sku-a", "label": "SKU A"},
                {"targetId": "target-b", "variantId": "sku-b", "label": "SKU B"},
            ],
            "config": {"repeats": 5, "seed": 13},
        },
        session,
    )
    experiment = create_experiment(session, request)
    start_experiment(session, experiment)
    attempts = list(
        session.scalars(
            select(AttemptRecord)
            .where(AttemptRecord.experiment_id == experiment.id)
            .order_by(AttemptRecord.queue_sequence)
        )
    )
    assert len(attempts) == 10
    scheduled = [
        (
            session.get(EvaluationRecord, attempt.evaluation_id).target_id,
            attempt.repeat_index,
        )
        for attempt in attempts
    ]
    for offset in range(0, len(scheduled), 2):
        assert {item[0] for item in scheduled[offset : offset + 2]} == {
            "target-a",
            "target-b",
        }
        assert len({item[1] for item in scheduled[offset : offset + 2]}) == 1
    assert scheduled[0][0] != scheduled[2][0]
    for attempt in attempts:
        evaluation = session.get(EvaluationRecord, attempt.evaluation_id)
        assert evaluation is not None
        base = 1000 if evaluation.target_id == "target-a" else 1100
        values = {
            "committed_tps": (base + attempt.repeat_index, "transactions/second", "rate"),
            "latency_p99_ms": (40, "ms", "p99"),
            "error_ratio": (0.0001, "ratio", "rate"),
            "abort_ratio": (0.002, "ratio", "rate"),
        }
        attempt.status = AttemptStatus.SUCCEEDED
        attempt.completed_at = utc_now()
        for metric, (value, unit, statistic) in values.items():
            session.add(
                ObservationRecord(
                    id=new_id("obs"),
                    attempt_id=attempt.id,
                    metric=metric,
                    value_number=float(value),
                    value_boolean=None,
                    unit=unit,
                    phase="measurement",
                    workload=evaluation.workload_id,
                    sample_index=None,
                    sample_count=120000,
                    statistic=statistic,
                    timestamp_text=None,
                    attributes_json={},
                    created_at=utc_now(),
                )
            )
    session.flush()
    result = build_analysis_snapshot(session, experiment.id, persist=False)
    assert result["mode"] == "selection"
    assert len(result["targets"]) == 2
    comparison = result["comparisons"][0]
    assert comparison["inference_unit"] == "time_block"
    assert comparison["winner"] == "sku-b"
    assert comparison["distinguishable"] is True
    assert comparison["conclusion_strength"] == "single-placement-provisional"


def test_selection_frontier_persists_and_appends_one_paired_load_batch(
    db_session: object,
) -> None:
    session = db_session
    capabilities = ["linux", "x86_64", "container", "benchbase", "postgresql"]
    for target_id in ["frontier-a", "frontier-b"]:
        snapshot = {
            "provider": "fixture",
            "capabilities": capabilities,
            "fingerprint": {"processor": target_id, "logical_cpu_count": 8},
        }
        session.add(
            TargetRecord(
                id=target_id,
                name=target_id,
                provider="fixture",
                status="available",
                capabilities_json=capabilities,
                inventory_json={"instance_type": target_id},
                fingerprint_json=snapshot["fingerprint"],
                snapshot_digest=canonical_digest(snapshot),
                runnable=True,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
    benchmark = session.scalar(
        select(BenchmarkRecord).where(
            BenchmarkRecord.benchmark_id == "benchbase.smallbank.postgres"
        )
    )
    assert benchmark is not None
    benchmark.manifest_json["spec"]["x-extensions"]["executionStatus"] = "executable"
    benchmark.manifest_json["spec"]["runtime"]["image"] = (
        "example.invalid/benchbase@sha256:" + "3" * 64
    )
    request = _normalize_create_request(
        {
            "mode": "selection",
            "name": "Adaptive frontier fixture",
            "benchmarkId": benchmark.benchmark_id,
            "targetIds": ["frontier-a", "frontier-b"],
            "targetBindings": [
                {
                    "targetId": "frontier-a",
                    "variantId": "sku-a",
                    "label": "SKU A",
                },
                {
                    "targetId": "frontier-b",
                    "variantId": "sku-b",
                    "label": "SKU B",
                },
            ],
            "config": {"repeats": 5, "referenceOfferedLoad": 100, "seed": 31},
        },
        session,
    )
    experiment = create_experiment(session, request)
    start_experiment(session, experiment)
    initial_points = list(
        session.scalars(
            select(SelectionLoadPointRecord)
            .where(SelectionLoadPointRecord.experiment_id == experiment.id)
            .order_by(SelectionLoadPointRecord.sequence)
        )
    )
    assert [float(point.offered_load) for point in initial_points] == [50, 75, 100]
    assert all(point.origin == "initial" for point in initial_points)
    initial_attempts = list(
        session.scalars(
            select(AttemptRecord).where(AttemptRecord.experiment_id == experiment.id)
        )
    )
    assert len(initial_attempts) == 30
    point_by_id = {point.id: point for point in initial_points}
    metrics = {
        "committed_tps": ("transactions/second", "rate", None),
        "latency_p99_ms": ("ms", "p99", 120000),
        "error_ratio": ("ratio", "rate", None),
        "abort_ratio": ("ratio", "rate", None),
        "timeout_ratio": ("ratio", "rate", None),
        "offered_load_achieved_ratio": ("ratio", "rate", None),
        "rate_limiter_lag_ratio": ("ratio", "rate", None),
        "client_headroom_ratio": ("ratio", "rate", None),
    }
    for attempt in initial_attempts:
        point = point_by_id[attempt.selection_load_point_id]
        offered_load = float(point.offered_load)
        values = {
            "committed_tps": offered_load * 0.995,
            "latency_p99_ms": 40 if offered_load < 100 else 65,
            "error_ratio": 0.0001,
            "abort_ratio": 0.002,
            "timeout_ratio": 0,
            "offered_load_achieved_ratio": 0.995,
            "rate_limiter_lag_ratio": 0.001,
            "client_headroom_ratio": 0.30,
        }
        attempt.status = AttemptStatus.SUCCEEDED
        attempt.completed_at = utc_now()
        for metric, value in values.items():
            unit, statistic, sample_count = metrics[metric]
            session.add(
                ObservationRecord(
                    id=new_id("obs"),
                    attempt_id=attempt.id,
                    metric=metric,
                    value_number=float(value),
                    value_boolean=None,
                    unit=unit,
                    phase="measurement",
                    workload="smallbank-postgres-serializable",
                    sample_index=None,
                    sample_count=sample_count,
                    statistic=statistic,
                    timestamp_text=None,
                    attributes_json={},
                    created_at=utc_now(),
                )
            )
        for check_id, kind in [("accounting", "correctness"), ("client", "resource")]:
            session.add(
                CheckRecord(
                    id=new_id("chk"),
                    attempt_id=attempt.id,
                    check_id=check_id,
                    passed=True,
                    scope="block",
                    kind=kind,
                    message=None,
                    details_json={},
                    created_at=utc_now(),
                )
            )
    session.flush()

    advance_experiment(session, experiment.id)
    points = list(
        session.scalars(
            select(SelectionLoadPointRecord)
            .where(SelectionLoadPointRecord.experiment_id == experiment.id)
            .order_by(SelectionLoadPointRecord.sequence)
        )
    )
    assert len(points) == 4
    adaptive = points[-1]
    assert adaptive.origin == "adaptive"
    assert float(adaptive.offered_load) == 87.5
    adaptive_attempts = list(
        session.scalars(
            select(AttemptRecord)
            .where(AttemptRecord.selection_load_point_id == adaptive.id)
            .order_by(AttemptRecord.queue_sequence)
        )
    )
    assert len(adaptive_attempts) == 10
    assert min(item.queue_sequence for item in adaptive_attempts) > max(
        item.queue_sequence for item in initial_attempts
    )
    for offset in range(0, len(adaptive_attempts), 2):
        evaluations = {
            session.get(EvaluationRecord, item.evaluation_id).target_id
            for item in adaptive_attempts[offset : offset + 2]
        }
        assert evaluations == {"frontier-a", "frontier-b"}

    advance_experiment(session, experiment.id)
    assert len(
        list(
            session.scalars(
                select(SelectionLoadPointRecord).where(
                    SelectionLoadPointRecord.experiment_id == experiment.id
                )
            )
        )
    ) == 4
