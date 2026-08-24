from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest
from looper_api.analysis_service import build_analysis_snapshot
from looper_api.app import _normalize_create_request, list_benchmarks
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
    create_demo_request,
    create_experiment,
    start_experiment,
)
from looper_api.serialization import benchmark_view
from looper_core.canonical import canonical_digest, new_id, utc_now
from looper_core.state import AttemptStatus
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
    assert "looper.phoronix-phpbench" in views
    phoronix = views["looper.phoronix-phpbench"]
    assert phoronix["selectionReady"] is True
    assert phoronix["runnable"] is True
    benchbase = views["benchbase.smallbank.postgres"]
    assert benchbase["category"] == "scenario"
    assert benchbase["executionStatus"] == "stage0-adapter-only"
    assert benchbase["runnable"] is False
    assert benchbase["primaryMetric"] == "committed_tps"


def test_benchmark_view_exposes_metric_presentation_without_dropping_metrics(
    db_session: object,
) -> None:
    session = db_session
    records = list(session.scalars(select(BenchmarkRecord).order_by(BenchmarkRecord.key)))
    views = {record.benchmark_id: benchmark_view(record) for record in records}

    benchbase = views["benchbase.smallbank.postgres"]
    # The legacy metrics string list is preserved for existing clients.
    assert "committed_tps" in benchbase["metrics"]
    # Structured definitions are additive.
    assert "metricDefinitions" in benchbase
    committed = benchbase["metricDefinitions"]["committed_tps"]
    assert committed["unit"] == "transactions/second"
    assert committed["presentation"]["roles"] == ["primary_outcome"]
    assert committed["presentation"]["defaultVisibility"] == "summary"

    # A context metric is machine-readable and not presented as a hard gate.
    offered = benchbase["metricDefinitions"]["offered_tps"]
    assert offered["presentation"]["roles"] == ["context"]

    # A hard gate role is a display hint and never a substitute for scenario gates.
    p99 = benchbase["metricDefinitions"]["latency_p99_ms"]
    assert "hard_gate" in p99["presentation"]["roles"]
    assert "guardrail" in p99["presentation"]["roles"]

    # Primary metric appears as a primary_outcome in its presentation semantics.
    assert "primary_outcome" in committed["presentation"]["roles"]


def test_catalog_exposes_only_current_version_per_benchmark_id(db_session: object) -> None:
    session = db_session
    current = session.scalar(
        select(BenchmarkRecord).where(BenchmarkRecord.benchmark_id == "looper.sysbench")
    )
    assert current is not None
    old_manifest = deepcopy(current.manifest_json)
    old_manifest["metadata"]["version"] = "0.9.0"
    session.add(BenchmarkRecord(
        key="looper.sysbench@0.9.0",
        benchmark_id=current.benchmark_id,
        version="0.9.0",
        name=current.name,
        description=current.description,
        license=current.license,
        manifest_digest="sha256:" + "0" * 64,
        manifest_json=old_manifest,
        manifest_path=current.manifest_path,
        package_digest=current.package_digest,
        trusted=current.trusted,
        installed_at=current.installed_at - timedelta(days=1),
    ))
    session.flush()

    catalog = list_benchmarks(session)
    sysbench = [item for item in catalog["items"] if item["id"] == "looper.sysbench"]
    assert len(sysbench) == 1
    assert sysbench[0]["version"] == current.version

    with pytest.raises(SchedulerError, match="has been replaced"):
        _normalize_create_request(
            {
                "mode": "selection",
                "name": "obsolete Sysbench",
                "benchmarkId": "looper.sysbench",
                "benchmarkVersion": "0.9.0",
                "targetIds": ["local"],
            },
            session,
        )


def test_inactive_target_cannot_be_selected_for_a_new_experiment(db_session: object) -> None:
    target = db_session.get(TargetRecord, "local")
    target.lifecycle_status = "archived"

    with pytest.raises(SchedulerError, match="archived and cannot be selected"):
        create_experiment(db_session, create_demo_request("inactive target"))


def test_stage0_adapter_cannot_be_selected_for_a_new_study(
    db_session: object,
) -> None:
    session = db_session
    with pytest.raises(SchedulerError, match="not directly testable"):
        _normalize_create_request(
            {
                "mode": "selection",
                "name": "SmallBank SKU pilot",
                "benchmarkId": "benchbase.smallbank.postgres",
                "targetIds": ["local"],
                "config": {"repeats": 5, "seed": 77, "timeout": 86400},
            },
            session,
        )


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
    benchmark.manifest_json["spec"]["x-extensions"]["selectable"] = True
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


def _create_frontier_fixture(
    session: object,
    *,
    max_attempts: int = 100,
    wall_time_seconds: int = 86400,
) -> tuple[object, list[SelectionLoadPointRecord], list[AttemptRecord]]:
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
    benchmark.manifest_json["spec"]["x-extensions"]["selectable"] = True
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
            "config": {
                "repeats": 5,
                "referenceOfferedLoad": 100,
                "seed": 31,
                "timeout": wall_time_seconds,
            },
        },
        session,
    )
    request.spec.budget.max_attempts = max_attempts
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
        session.scalars(select(AttemptRecord).where(AttemptRecord.experiment_id == experiment.id))
    )
    assert len(initial_attempts) == 30
    return experiment, initial_points, initial_attempts


def _complete_frontier_attempts(
    session: object,
    points: list[SelectionLoadPointRecord],
    attempts: list[AttemptRecord],
) -> None:
    point_by_id = {point.id: point for point in points}
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
    for attempt in attempts:
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


def test_selection_frontier_persists_and_appends_one_paired_load_batch(
    db_session: object,
) -> None:
    session = db_session
    experiment, initial_points, initial_attempts = _create_frontier_fixture(session)
    _complete_frontier_attempts(session, initial_points, initial_attempts)

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
    assert (
        len(
            list(
                session.scalars(
                    select(SelectionLoadPointRecord).where(
                        SelectionLoadPointRecord.experiment_id == experiment.id
                    )
                )
            )
        )
        == 4
    )


@pytest.mark.parametrize(
    ("expected_reason", "max_attempts", "wall_time_seconds"),
    [
        ("attempt_budget_exhausted", 30, 86400),
        ("wall_time_budget_exhausted", 100, 1),
    ],
)
def test_selection_frontier_finishes_unresolved_when_budget_cannot_fit_next_batch(
    db_session: object,
    expected_reason: str,
    max_attempts: int,
    wall_time_seconds: int,
) -> None:
    session = db_session
    experiment, initial_points, initial_attempts = _create_frontier_fixture(
        session,
        max_attempts=max_attempts,
        wall_time_seconds=wall_time_seconds,
    )
    _complete_frontier_attempts(session, initial_points, initial_attempts)
    if expected_reason == "wall_time_budget_exhausted":
        experiment.started_at = utc_now() - timedelta(seconds=wall_time_seconds + 1)

    advance_experiment(session, experiment.id)

    points = list(
        session.scalars(
            select(SelectionLoadPointRecord)
            .where(SelectionLoadPointRecord.experiment_id == experiment.id)
            .order_by(SelectionLoadPointRecord.sequence)
        )
    )
    assert len(points) == 3
    assert points[-1].analysis_json["frontier_status"] == "frontier_unresolved"
    assert points[-1].analysis_json["termination_reason"] == expected_reason
    snapshot = build_analysis_snapshot(session, experiment.id, persist=False)
    assert snapshot["frontier"]["status"] == "frontier_unresolved"
    assert snapshot["frontier"]["termination_reason"] == expected_reason
    assert len(snapshot["frontier"]["trajectory"]) == 3
    assert sum(len(point["attempts"]) for point in snapshot["frontier"]["trajectory"]) == 30


def test_selection_frontier_preserves_failed_attempt_trajectory(db_session: object) -> None:
    session = db_session
    experiment, initial_points, initial_attempts = _create_frontier_fixture(session)
    spec = dict(experiment.spec_json)
    spec["design"] = {**spec["design"], "max_retries": 0}
    experiment.spec_json = spec
    _complete_frontier_attempts(session, initial_points, initial_attempts)
    failed_attempt = initial_attempts[0]
    failed_attempt.status = AttemptStatus.FAILED
    failed_attempt.error_message = "fixture load generator failed"

    advance_experiment(session, experiment.id)

    failed_point = session.get(SelectionLoadPointRecord, failed_attempt.selection_load_point_id)
    assert failed_point is not None
    assert failed_point.analysis_json["reason"] == "repeat_failures_exhausted"
    assert failed_point.analysis_json["frontier_status"] == "frontier_unresolved"
    snapshot = build_analysis_snapshot(session, experiment.id, persist=False)
    failed_facts = [
        attempt
        for point in snapshot["frontier"]["trajectory"]
        for attempt in point["attempts"]
        if attempt["attempt_id"] == failed_attempt.id
    ]
    assert failed_facts == [
        {
            "attempt_id": failed_attempt.id,
            "target_id": session.get(EvaluationRecord, failed_attempt.evaluation_id).target_id,
            "repeat_index": failed_attempt.repeat_index,
            "retry_index": failed_attempt.retry_index,
            "queue_sequence": failed_attempt.queue_sequence,
            "status": AttemptStatus.FAILED,
            "error_message": "fixture load generator failed",
        }
    ]
