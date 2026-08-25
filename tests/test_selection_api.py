from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest
from looper_api.analysis_service import build_analysis_snapshot
from looper_api.app import _normalize_create_request, list_benchmarks
from looper_api.models import (
    AttemptRecord,
    BenchmarkRecord,
    CandidateRecord,
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
from looper_api.serialization import benchmark_view, experiment_view
from looper_core.canonical import canonical_digest, new_id, utc_now
from looper_core.state import AttemptStatus
from sqlalchemy import select


def test_scenario_catalog_exposes_execution_boundary(db_session: object) -> None:
    session = db_session
    for benchmark_id in (
        "benchbase.smallbank.postgres",
        "looper.fixture.config-driven",
        "looper.demo.compression",
    ):
        record = session.scalar(
            select(BenchmarkRecord).where(BenchmarkRecord.benchmark_id == benchmark_id)
        )
        session.delete(record)
    session.flush()
    records = list(session.scalars(select(BenchmarkRecord).order_by(BenchmarkRecord.key)))
    views = {record.benchmark_id: benchmark_view(record) for record in records}
    assert set(views) >= {"dcperf.mediawiki.closed-loop", "looper.sysbench"}
    assert {
        "benchbase.smallbank.postgres",
        "looper.fixture.config-driven",
        "looper.demo.compression",
    }.isdisjoint(views)
    assert "looper.phoronix-phpbench" in views
    phoronix = views["looper.phoronix-phpbench"]
    assert phoronix["selectionReady"] is True
    assert phoronix["singleNodeReady"] is True
    assert phoronix["runnable"] is True
    assert phoronix["resultSections"] == [
        {
            "id": "phpbench-results",
            "label": "PHPBench 数据",
            "description": (
                "展示固定 PTS profile 的综合分数、重复样本、执行参数、"
                "协议门禁和原始证据状态。"
            ),
            "view": "phpbench-results",
            "metrics": [
                "phpbench_score",
                "phpbench_score_sample",
                "sample_count",
                "pts_run_ok",
                "profile_version_match",
            ],
        }
    ]
    assert "auditStatus" not in views["dcperf.mediawiki.closed-loop"]


def test_selection_defaults_are_benchmark_specific_and_used_by_create_request(
    db_session: object,
) -> None:
    session = db_session
    records = {record.benchmark_id: record for record in session.scalars(select(BenchmarkRecord))}
    views = {benchmark_id: benchmark_view(record) for benchmark_id, record in records.items()}

    sysbench_defaults = views["looper.sysbench"]["selectionDefaults"]
    dcperf_defaults = views["dcperf.mediawiki.closed-loop"]["selectionDefaults"]
    assert sysbench_defaults == {"repeats": 5, "timeout": 3600, "seed": 20260301}
    assert dcperf_defaults == {"repeats": 5, "timeout": 86400, "seed": 20260306}
    assert sysbench_defaults != dcperf_defaults

    sysbench = records["looper.sysbench"]
    request = _normalize_create_request(
        {
            "mode": "selection",
            "name": "Sysbench defaults",
            "benchmarkId": sysbench.benchmark_id,
            "benchmarkVersion": sysbench.version,
            "targetIds": ["local"],
        },
        session,
    )
    assert request.spec.design.min_repeats == 5
    assert request.spec.budget.wall_time_seconds == 3600
    assert request.spec.design.random_seed == 20260301

    experiment = create_experiment(session, request)
    start_experiment(session, experiment)
    candidate = session.scalar(
        select(CandidateRecord).where(CandidateRecord.experiment_id == experiment.id)
    )
    assert candidate is not None
    assert candidate.parameters_json == {"threads": 4, "time": 10}


def test_selection_analysis_accepts_boolean_scenario_flags(db_session: object) -> None:
    session = db_session
    benchmark = session.scalar(
        select(BenchmarkRecord).where(BenchmarkRecord.benchmark_id == "looper.phoronix-phpbench")
    )
    assert benchmark is not None
    request = _normalize_create_request(
        {
            "mode": "selection",
            "name": "PHPBench boolean gate fixture",
            "benchmarkId": benchmark.benchmark_id,
            "benchmarkVersion": benchmark.version,
            "targetIds": ["local"],
            "config": {"repeats": 3},
        },
        session,
    )
    experiment = create_experiment(session, request)
    start_experiment(session, experiment)
    attempts = list(
        session.scalars(
            select(AttemptRecord)
            .where(AttemptRecord.experiment_id == experiment.id)
            .order_by(AttemptRecord.repeat_index)
        )
    )
    assert len(attempts) == 3
    for index, attempt in enumerate(attempts):
        evaluation = session.get(EvaluationRecord, attempt.evaluation_id)
        assert evaluation is not None
        attempt.status = AttemptStatus.SUCCEEDED
        attempt.completed_at = utc_now()
        for metric, value, unit in (
            ("pts_run_ok", True, "flag"),
            ("profile_version_match", True, "flag"),
            ("phpbench_score", 100.0 + index, "Score"),
        ):
            session.add(
                ObservationRecord(
                    id=new_id("obs"),
                    attempt_id=attempt.id,
                    metric=metric,
                    value_number=None if isinstance(value, bool) else float(value),
                    value_boolean=value if isinstance(value, bool) else None,
                    unit=unit,
                    phase="measurement",
                    workload=evaluation.workload_id,
                    sample_index=None,
                    sample_count=3,
                    statistic="sample",
                    timestamp_text=None,
                    attributes_json={},
                    created_at=utc_now(),
                )
            )
    session.flush()

    result = build_analysis_snapshot(session, experiment.id, persist=False)
    target = result["targets"][0]
    assert target["valid_block_count"] == 3
    assert target["metrics"][0]["raw"] == 101.0


def test_benchmark_view_exposes_metric_presentation_without_dropping_metrics(
    db_session: object,
) -> None:
    session = db_session
    records = list(session.scalars(select(BenchmarkRecord).order_by(BenchmarkRecord.key)))
    views = {record.benchmark_id: benchmark_view(record) for record in records}

    sysbench = views["looper.sysbench"]
    # The legacy metrics string list is preserved for existing clients.
    assert "events_per_sec" in sysbench["metrics"]
    # Structured definitions are additive.
    assert "metricDefinitions" in sysbench
    events = sysbench["metricDefinitions"]["events_per_sec"]
    assert events["unit"] == "events/s"
    assert events["presentation"]["roles"] == ["primary_outcome"]
    assert events["presentation"]["defaultVisibility"] == "summary"

    # A hard gate role is a display hint and never a substitute for scenario gates.
    run_ok = sysbench["metricDefinitions"]["sysbench_run_ok"]
    assert "hard_gate" in run_ok["presentation"]["roles"]

    # Primary metric appears as a primary_outcome in its presentation semantics.
    assert "primary_outcome" in events["presentation"]["roles"]


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


def test_experiment_view_keeps_completed_repeat_metrics_while_later_repeats_are_queued(
    db_session: object,
) -> None:
    session = db_session
    experiment = create_experiment(session, create_demo_request("Result repeat visibility"))
    start_experiment(session, experiment)
    attempts = list(
        session.scalars(
            select(AttemptRecord)
            .where(AttemptRecord.experiment_id == experiment.id)
            .order_by(AttemptRecord.queue_sequence)
        )
    )
    first = attempts[0]
    first.status = AttemptStatus.SUCCEEDED
    assert first.repeat_index == 0
    same_evaluation = [item for item in attempts if item.evaluation_id == first.evaluation_id]
    assert [item.repeat_index for item in same_evaluation] == [0, 1, 2]
    first.status = AttemptStatus.SUCCEEDED
    first.completed_at = utc_now()
    session.add(CheckRecord(
        id=new_id("chk"), attempt_id=first.id, check_id="roundtrip", passed=True,
        scope="attempt", kind="correctness", message=None, details_json={},
        created_at=utc_now(),
    ))
    session.add(
        ObservationRecord(
            id=new_id("obs"),
            attempt_id=first.id,
            metric="throughput_mib_s",
            value_number=321.5,
            value_boolean=None,
            unit="MiB/s",
            phase="measurement",
            workload="corpus-small",
            sample_index=None,
            sample_count=3,
            statistic="median",
            timestamp_text=None,
            attributes_json={},
            created_at=utc_now(),
        )
    )
    session.flush()

    view = experiment_view(session, experiment, detail=True)
    evaluation = next(item for item in view["evaluations"] if item["id"] == first.evaluation_id)

    assert evaluation["attemptId"] == same_evaluation[-1].id
    assert evaluation["resultAttemptId"] == first.id
    assert any(
        item["name"] == "throughput_mib_s" and item["value"] == 321.5
        for item in evaluation["metrics"]
    )


def test_experiment_view_averages_successful_repeat_metrics(db_session: object) -> None:
    session = db_session
    benchmark = session.scalar(
        select(BenchmarkRecord).where(
            BenchmarkRecord.benchmark_id == "looper.phoronix-phpbench"
        )
    )
    assert benchmark is not None
    request = _normalize_create_request(
        {
            "mode": "selection",
            "name": "PHPBench sample visibility",
            "benchmarkId": benchmark.benchmark_id,
            "benchmarkVersion": benchmark.version,
            "targetIds": ["local"],
            "config": {"repeats": 3},
        },
        session,
    )
    experiment = create_experiment(session, request)
    start_experiment(session, experiment)
    attempts = list(
        session.scalars(
            select(AttemptRecord)
            .where(AttemptRecord.experiment_id == experiment.id)
            .order_by(AttemptRecord.repeat_index)
        )
    )
    assert len(attempts) == 3
    for attempt, value in zip(attempts, (517432.0, 528171.0, 525322.0), strict=True):
        attempt.status = AttemptStatus.SUCCEEDED
        attempt.completed_at = utc_now()
        for check_id in ("pts-run-ok", "profile-contract-match"):
            session.add(CheckRecord(
                id=new_id("chk"), attempt_id=attempt.id, check_id=check_id, passed=True,
                scope="attempt", kind="correctness", message=None, details_json={},
                created_at=utc_now(),
            ))
        session.add(
            ObservationRecord(
                id=new_id("obs"),
                attempt_id=attempt.id,
                metric="phpbench_score",
                value_number=value,
                value_boolean=None,
                unit="Score",
                phase="measurement",
                workload="phpbench",
                sample_index=None,
                sample_count=None,
                statistic="median",
                timestamp_text=None,
                attributes_json={},
                created_at=utc_now(),
            )
        )
    session.flush()

    view = experiment_view(session, experiment, detail=True)
    scores = [
        metric
        for metric in view["evaluations"][0]["metrics"]
        if metric["name"] == "phpbench_score"
    ]
    assert len(scores) == 1
    assert scores[0]["value"] == pytest.approx(523641.6666666667)
    assert scores[0]["sampleIndex"] is None
    assert scores[0]["sampleCount"] == 3
    assert scores[0]["statistic"] == "mean"


def test_experiment_view_excludes_failed_rounds_from_metric_average(
    db_session: object,
) -> None:
    session = db_session
    experiment = create_experiment(session, create_demo_request("Valid repeat average"))
    start_experiment(session, experiment)
    attempts = list(
        session.scalars(
            select(AttemptRecord)
            .where(AttemptRecord.experiment_id == experiment.id)
            .order_by(AttemptRecord.queue_sequence)
        )
    )
    first = attempts[0]
    same_evaluation = [item for item in attempts if item.evaluation_id == first.evaluation_id]
    succeeded, failed = same_evaluation[:2]
    for attempt, status, value, passed in (
        (succeeded, AttemptStatus.SUCCEEDED, 100.0, True),
        (failed, AttemptStatus.FAILED, 900.0, False),
    ):
        attempt.status = status
        attempt.completed_at = utc_now()
        session.add(ObservationRecord(
            id=new_id("obs"), attempt_id=attempt.id, metric="throughput_mib_s",
            value_number=value, value_boolean=None, unit="MiB/s", phase="measurement",
            workload="corpus-small", sample_index=None, sample_count=1,
            statistic="median", timestamp_text=None, attributes_json={}, created_at=utc_now(),
        ))
        session.add(CheckRecord(
            id=new_id("chk"), attempt_id=attempt.id, check_id="roundtrip", passed=passed,
            scope="attempt", kind="correctness", message=None, details_json={},
            created_at=utc_now(),
        ))
    session.flush()

    view = experiment_view(session, experiment, detail=True)
    evaluation = next(item for item in view["evaluations"] if item["id"] == first.evaluation_id)
    throughput = next(
        item for item in evaluation["metrics"] if item["name"] == "throughput_mib_s"
    )
    assert throughput["value"] == 100.0
    assert [run["status"] for run in evaluation["runs"][:2]] == ["completed", "failed"]


def test_experiment_view_keeps_all_declared_sample_metrics(db_session: object) -> None:
    session = db_session
    benchmark = session.scalar(
        select(BenchmarkRecord).where(
            BenchmarkRecord.benchmark_id == "looper.phoronix-phpbench"
        )
    )
    assert benchmark is not None
    request = _normalize_create_request(
        {
            "mode": "selection",
            "name": "PHPBench sample visibility",
            "benchmarkId": benchmark.benchmark_id,
            "benchmarkVersion": benchmark.version,
            "targetIds": ["local"],
            "config": {"repeats": 3},
        },
        session,
    )
    experiment = create_experiment(session, request)
    start_experiment(session, experiment)
    attempt = session.scalar(
        select(AttemptRecord)
        .where(AttemptRecord.experiment_id == experiment.id)
        .order_by(AttemptRecord.repeat_index)
    )
    assert attempt is not None
    attempt.status = AttemptStatus.SUCCEEDED
    attempt.completed_at = utc_now()
    for check_id in ("pts-run-ok", "profile-contract-match"):
        session.add(CheckRecord(
            id=new_id("chk"), attempt_id=attempt.id, check_id=check_id, passed=True,
            scope="attempt", kind="correctness", message=None, details_json={},
            created_at=utc_now(),
        ))
    for index, value in enumerate((517432.0, 528171.0, 525322.0)):
        session.add(
            ObservationRecord(
                id=new_id("obs"),
                attempt_id=attempt.id,
                metric="phpbench_score_sample",
                value_number=value,
                value_boolean=None,
                unit="Score",
                phase="measurement",
                workload="phpbench",
                sample_index=index,
                sample_count=3,
                statistic="sample",
                timestamp_text=None,
                attributes_json={},
                created_at=utc_now(),
            )
        )
    session.flush()

    view = experiment_view(session, experiment, detail=True)
    samples = [
        metric
        for metric in view["evaluations"][0]["metrics"]
        if metric["name"] == "phpbench_score_sample"
    ]
    assert [sample["value"] for sample in samples] == [517432.0, 528171.0, 525322.0]
    assert [sample["sampleIndex"] for sample in samples] == [0, 1, 2]
    assert all(sample["sampleCount"] == 3 for sample in samples)


def test_removed_adapter_cannot_be_selected_for_a_new_study(
    db_session: object,
) -> None:
    session = db_session
    session.delete(session.scalar(select(BenchmarkRecord).where(
        BenchmarkRecord.benchmark_id == "benchbase.smallbank.postgres"
    )))
    session.flush()
    with pytest.raises(SchedulerError, match="not installed"):
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


@pytest.mark.skip(reason="multi-target selection returns when multi-node slot scheduling is added")
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


@pytest.mark.skip(reason="multi-target selection returns when multi-node slot scheduling is added")
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
@pytest.mark.skip(reason="multi-target selection returns when multi-node slot scheduling is added")
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


@pytest.mark.skip(reason="multi-target selection returns when multi-node slot scheduling is added")
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
