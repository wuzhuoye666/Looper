from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from looper_api.app import _normalize_create_request
from looper_api.models import (
    AttemptRecord,
    BenchmarkRecord,
    CandidateRecord,
    CheckRecord,
    EvaluationRecord,
    ObservationRecord,
    TargetRecord,
)
from looper_api.scheduler import create_experiment, start_experiment
from looper_api.serialization import (
    _best_primary_score,
    _comparison_axes,
    _iso,
    _normalize_comparison_values,
    _observation_metric_view,
    _scenario_comparison_views,
    _vgo_conclusion,
    dashboard_view,
    experiment_view,
)
from looper_core.canonical import canonical_digest, new_id, utc_now
from looper_core.contracts import Direction
from looper_core.state import AttemptStatus, CandidateStatus, ExperimentStatus
from sqlalchemy import select


def test_iso_marks_database_timestamps_as_utc() -> None:
    timestamp = datetime(2026, 8, 24, 1, 53, 23, 940536)

    assert _iso(timestamp) == "2026-08-24T01:53:23.940536Z"
    assert _iso(timestamp.replace(tzinfo=UTC)) == "2026-08-24T01:53:23.940536Z"
    assert _iso(None) is None


RESULTS = [
    {
        "feasible": True,
        "pareto_rank": 1,
        "sequence": 0,
        "objectives": [{"metric": "throughput", "raw": 958.0}],
    },
    {
        "feasible": True,
        "pareto_rank": 1,
        "sequence": 1,
        "objectives": [{"metric": "throughput", "raw": 3963.0}],
    },
]


def test_best_primary_score_uses_objective_direction() -> None:
    assert _best_primary_score(RESULTS, "throughput", Direction.MAXIMIZE) == 3963.0
    assert _best_primary_score(RESULTS, "throughput", Direction.MINIMIZE) == 958.0


def test_best_primary_score_ignores_missing_and_boolean_values() -> None:
    assert _best_primary_score(RESULTS, "missing", Direction.MAXIMIZE) is None
    assert (
        _best_primary_score(
            [{"objectives": [{"metric": "valid", "raw": True}]}],
            "valid",
            Direction.MAXIMIZE,
        )
        is None
    )


def test_failed_attempt_observation_can_still_be_presented() -> None:
    observation = SimpleNamespace(
        metric="closed_loop_successful_rps",
        value_number=128.84,
        value_boolean=None,
        unit="requests/second",
        sample_index=None,
        sample_count=None,
        statistic="mean",
    )

    assert _observation_metric_view(observation, {"direction": "maximize"}) == {
        "name": "closed_loop_successful_rps",
        "value": 128.84,
        "unit": "requests/second",
        "sampleIndex": None,
        "sampleCount": None,
        "statistic": "mean",
        "baseline": None,
        "direction": "max",
    }


def test_comparison_normalization_respects_metric_direction() -> None:
    assert _normalize_comparison_values({"a": 50, "b": 100}, "maximize") == {
        "a": 50,
        "b": 100,
    }
    assert _normalize_comparison_values({"a": 5, "b": 10}, "minimize") == {
        "a": 100,
        "b": 50,
    }
    assert _normalize_comparison_values({"a": 0, "b": 0}, "maximize") == {
        "a": 100,
        "b": 100,
    }
    assert _normalize_comparison_values({"a": 0, "b": 10}, "minimize") == {}


def test_comparison_axes_fall_back_to_unique_global_primary_outcome() -> None:
    manifest = {
        "spec": {
            "workloads": [{"id": "mediawiki", "name": "MediaWiki MLP with wrk"}],
            "metrics": {
                "closed_loop_successful_rps": {
                    "unit": "requests/second",
                    "direction": "maximize",
                    "presentation": {"roles": ["primary_outcome"]},
                },
                "latency_p95_ms": {
                    "unit": "ms",
                    "direction": "minimize",
                    "presentation": {"roles": ["guardrail"]},
                },
            },
        }
    }

    assert _comparison_axes(manifest) == [
        {
            "key": "mediawiki",
            "workloadId": "mediawiki",
            "label": "MediaWiki MLP with wrk",
            "metric": "closed_loop_successful_rps",
            "unit": "requests/second",
            "direction": "maximize",
        }
    ]


def test_dcperf_comparison_axes_use_throughput_and_latency_profile() -> None:
    manifest = {
        "metadata": {"id": "dcperf.mediawiki.closed-loop"},
        "spec": {
            "workloads": [{"id": "mediawiki", "name": "MediaWiki"}],
            "metrics": {
                "closed_loop_successful_rps": {
                    "unit": "requests/second",
                    "direction": "maximize",
                },
                "latency_p50_ms": {"unit": "ms", "direction": "minimize"},
                "latency_p95_ms": {"unit": "ms", "direction": "minimize"},
                "latency_p99_ms": {"unit": "ms", "direction": "minimize"},
            },
        },
    }

    axes = _comparison_axes(manifest)

    assert [axis["key"] for axis in axes] == [
        "closed_loop_successful_rps",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
    ]
    assert [axis["label"] for axis in axes] == [
        "成功请求率",
        "P50 延迟",
        "P95 延迟",
        "P99 延迟",
    ]
    assert [axis["direction"] for axis in axes] == [
        "maximize",
        "minimize",
        "minimize",
        "minimize",
    ]


def test_vgo_comparison_axes_keep_four_key_results_per_workload() -> None:
    manifest = {
        "metadata": {"id": "looper.vgo.variability"},
        "spec": {
            "workloads": [
                {"id": "matmul", "name": "Matmul 内存分配波动"},
                {"id": "7z", "name": "7-Zip 单线程波动"},
            ],
            "metrics": {
                "runtime_cv": {"unit": "ratio", "direction": "minimize"},
                "optimized_runtime_cv": {"unit": "ratio", "direction": "minimize"},
                "optimized_median_runtime_seconds": {"unit": "s", "direction": "minimize"},
                "optimized_p95_runtime_seconds": {"unit": "s", "direction": "minimize"},
                "cpu_steal_p95_percent": {"unit": "%", "direction": "minimize"},
            },
        },
    }

    axes = _comparison_axes(manifest)

    assert len(axes) == 8
    assert [axis["label"] for axis in axes[:4]] == [
        "基线 CV",
        "优化后 CV",
        "优化后中位耗时",
        "优化后 P95",
    ]
    assert axes[0]["key"] == "matmul:runtime_cv"
    assert axes[0]["workloadLabel"] == "Matmul 内存分配波动"
    assert axes[4]["key"] == "7z:runtime_cv"
    assert all(axis["metric"] != "cpu_steal_p95_percent" for axis in axes)


def test_vgo_conclusion_explains_whether_optimization_reduced_variability() -> None:
    result = _vgo_conclusion(
        [
            {
                "workloadId": "7z",
                "workloadLabel": "7-Zip 单线程波动",
                "metrics": {
                    "correctness_rate": {"value": 1.0},
                    "runtime_cv": {"value": 0.0028},
                    "optimized_runtime_cv": {"value": 0.0072},
                },
            }
        ]
    )

    assert result == "7-Zip：优化未改善波动（CV 0.28%→0.72%）"


def _add_target(session: object, target_id: str, name: str) -> None:
    fingerprint = {
        "system": "Linux",
        "architecture": "x86_64",
        "logical_cpu_count": 16,
        "memory_gib": 64,
    }
    capabilities = ["python", "local-process", "linux", "x86_64"]
    session.add(
        TargetRecord(
            id=target_id,
            name=name,
            provider="fixture",
            status="available",
            capabilities_json=capabilities,
            inventory_json={"source": "test"},
            fingerprint_json=fingerprint,
            snapshot_digest=canonical_digest({"fingerprint": fingerprint, "id": target_id}),
            runnable=True,
            lifecycle_status="active",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
    )
    session.flush()


def _completed_sysbench_study(
    session: object,
    *,
    target_id: str,
    name: str,
    values: dict[str, float],
    updated_at: datetime,
) -> object:
    benchmark = session.scalar(
        select(BenchmarkRecord).where(BenchmarkRecord.benchmark_id == "looper.sysbench")
    )
    request = _normalize_create_request(
        {
            "mode": "selection",
            "name": name,
            "benchmarkId": benchmark.benchmark_id,
            "benchmarkVersion": benchmark.version,
            "targetIds": [target_id],
            "config": {"repeats": 1},
        },
        session,
    )
    experiment = create_experiment(session, request)
    start_experiment(session, experiment)
    attempts = list(
        session.scalars(select(AttemptRecord).where(AttemptRecord.experiment_id == experiment.id))
    )
    for attempt in attempts:
        evaluation = session.get(EvaluationRecord, attempt.evaluation_id)
        primary_metric = (
            "throughput_mib_s" if evaluation.workload_id == "memory" else "events_per_sec"
        )
        primary_value = values[evaluation.workload_id]
        observations = [
            (
                primary_metric,
                primary_value,
                "MiB/s" if primary_metric == "throughput_mib_s" else "events/s",
            ),
            ("sysbench_run_ok", True, "flag"),
        ]
        if primary_metric != "events_per_sec":
            observations.append(("events_per_sec", primary_value * 1024, "events/s"))
        for metric, value, unit in observations:
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
                    sample_count=1,
                    statistic="mean",
                    timestamp_text=None,
                    attributes_json={},
                    created_at=updated_at,
                )
            )
        session.add(
            CheckRecord(
                id=new_id("chk"),
                attempt_id=attempt.id,
                check_id="sysbench-run-ok",
                passed=True,
                scope="attempt",
                kind="correctness",
                message=None,
                details_json={},
                created_at=updated_at,
            )
        )
        attempt.status = AttemptStatus.SUCCEEDED
        attempt.completed_at = updated_at
        evaluation.status = CandidateStatus.FEASIBLE
        evaluation.completed_at = updated_at
    for candidate in session.scalars(
        select(CandidateRecord).where(CandidateRecord.experiment_id == experiment.id)
    ):
        candidate.status = CandidateStatus.FEASIBLE
        candidate.completed_at = updated_at
    experiment.status = ExperimentStatus.COMPLETED
    experiment.updated_at = updated_at
    experiment.finished_at = updated_at
    session.flush()
    return experiment


def test_scenario_comparison_groups_versions_and_uses_study_median(db_session: object) -> None:
    session = db_session
    _add_target(session, "radar-a", "机器 A")
    _add_target(session, "radar-b", "机器 B")
    now = utc_now()
    studies = [
        _completed_sysbench_study(
            session,
            target_id="radar-a",
            name="A first",
            values={"cpu": 100, "memory": 100, "thread": 100, "mutex": 100},
            updated_at=now - timedelta(minutes=3),
        ),
        _completed_sysbench_study(
            session,
            target_id="radar-a",
            name="A second",
            values={"cpu": 300, "memory": 300, "thread": 300, "mutex": 300},
            updated_at=now - timedelta(minutes=2),
        ),
        _completed_sysbench_study(
            session,
            target_id="radar-b",
            name="B",
            values={"cpu": 400, "memory": 400, "thread": 400, "mutex": 400},
            updated_at=now - timedelta(minutes=1),
        ),
    ]
    invalid_study = _completed_sysbench_study(
        session,
        target_id="radar-a",
        name="invalid A evidence",
        values={"cpu": 1000, "memory": 1000, "thread": 1000, "mutex": 1000},
        updated_at=now - timedelta(seconds=30),
    )
    for evaluation in session.scalars(
        select(EvaluationRecord).where(EvaluationRecord.experiment_id == invalid_study.id)
    ):
        evaluation.status = CandidateStatus.INFEASIBLE

    current = session.scalar(
        select(BenchmarkRecord).where(BenchmarkRecord.benchmark_id == "looper.sysbench")
    )
    old_manifest = deepcopy(current.manifest_json)
    old_manifest["metadata"]["version"] = "0.9.0"
    session.add(
        BenchmarkRecord(
            key="looper.sysbench@0.9.0",
            benchmark_id=current.benchmark_id,
            version="0.9.0",
            name=current.name,
            description=current.description,
            license=current.license,
            manifest_digest="sha256:" + "9" * 64,
            manifest_json=old_manifest,
            manifest_path=current.manifest_path,
            package_digest=current.package_digest,
            trusted=current.trusted,
            installed_at=current.installed_at - timedelta(days=1),
        )
    )
    old_version_studies = [
        _completed_sysbench_study(
            session,
            target_id=target_id,
            name=f"old version {target_id}",
            values={"cpu": value, "memory": value, "thread": value, "mutex": value},
            updated_at=now - timedelta(seconds=offset),
        )
        for target_id, value, offset in (("radar-a", 777, 10), ("radar-b", 999, 0))
    ]
    for old_version_study in old_version_studies:
        old_spec = dict(old_version_study.spec_json)
        old_spec["benchmark_version"] = "0.9.0"
        old_version_study.spec_json = old_spec
    session.flush()

    detail = experiment_view(session, studies[0], detail=True)
    assert detail["resultConclusion"].startswith("4/4 项完成")
    assert {item["workload"]: item["status"] for item in detail["evaluations"]} == {
        "cpu": "completed",
        "memory": "completed",
        "thread": "completed",
        "mutex": "completed",
    }
    assert all(item["metrics"] for item in detail["evaluations"])

    result = _scenario_comparison_views(session, [*studies, invalid_study, *old_version_studies])

    assert len(result) == 2
    assert result[0]["benchmarkVersion"] == "0.9.0"
    comparison = next(item for item in result if item["benchmarkVersion"] == current.version)
    assert comparison["benchmarkVersion"] == current.version
    assert [axis["key"] for axis in comparison["axes"]] == ["cpu", "memory", "thread", "mutex"]
    targets = {target["targetId"]: target for target in comparison["targets"]}
    assert targets["radar-a"]["studyCount"] == 2
    assert targets["radar-a"]["validSampleCount"] == 8
    assert targets["radar-a"]["values"]["cpu"] == {
        "raw": 200,
        "normalized": 50,
        "studyCount": 2,
        "sampleCount": 2,
    }
    assert targets["radar-b"]["values"]["cpu"]["raw"] == 400
    assert targets["radar-b"]["values"]["cpu"]["normalized"] == 100

    dashboard = dashboard_view(session)
    assert dashboard["scenarioComparisons"][0]["benchmarkVersion"] == "0.9.0"
    assert "trend" in dashboard
