"""VGO variability analyzer: distribution chain, clues, attribution, comparison."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from looper_api.models import AnalysisSnapshotRecord, AttemptRecord, ObservationRecord
from looper_api.scheduler import create_demo_request, create_experiment, start_experiment
from looper_api.variability_service import build_variability_report
from looper_core.contracts import Direction
from looper_core.state import AttemptStatus
from looper_core.variability import (
    VARIABILITY_CODE_VERSION,
    RunSample,
    VariabilityPolicy,
    analyze_variability,
    compare_distributions,
)


def _sample(value: float, **kwargs) -> RunSample:
    return RunSample(runId=kwargs.pop("run_id", f"run-{value}"), value=value, **kwargs)


def _bimodal_samples() -> list[RunSample]:
    """Ten fast runs near 1.2s and eight slow runs near 1.8s (VGO-style modes)."""

    samples: list[RunSample] = []
    for index in range(10):
        samples.append(
            _sample(
                1.2 + (index % 3) * 0.01,
                run_id=f"fast-{index}",
                system_metrics={
                    "dtlb_miss_count": 1000 + index,
                    "numa_migration_count": 40 + index,
                    "cpu_migration_count": 20 + index,
                },
            )
        )
    for index in range(8):
        samples.append(
            _sample(
                1.8 + (index % 3) * 0.01,
                run_id=f"slow-{index}",
                system_metrics={
                    "dtlb_miss_count": 5000 + index * 10,
                    "numa_migration_count": 300 + index,
                    "cpu_migration_count": 200 + index,
                },
            )
        )
    return samples


def test_insufficient_samples_fail_closed() -> None:
    report = analyze_variability(
        [_sample(1.0), _sample(1.1), _sample(0.9)],
        metric="runtime",
        unit="second",
        direction=Direction.MINIMIZE,
        group_label="g",
    )
    assert report.status == "insufficient_evidence"
    assert report.stability["verdict"] == "insufficient_evidence"
    assert report.runs == []
    assert report.recommendations[0].action.startswith("把重复次数增加到至少")
    assert report.selection_impact["confidence"] == "insufficient"


def test_stable_distribution_reports_clean_verdict() -> None:
    values = [100.02, 100.18, 99.95, 100.11, 100.25, 99.98, 100.07, 100.14]
    report = analyze_variability(
        [_sample(value, run_id=f"r{index}") for index, value in enumerate(values)],
        metric="runtime",
        unit="millisecond",
        direction=Direction.MINIMIZE,
        group_label="g",
    )
    assert report.status == "stable"
    assert report.distribution.count == 8
    assert report.distribution.coefficient_of_variation is not None
    assert report.distribution.coefficient_of_variation < 0.05
    assert report.outliers == {"slow": [], "fast": []}
    assert report.modes is None
    assert report.association_clues == []
    # No system metrics collected -> the analyzer must say so explicitly.
    assert any("系统指标" in item.action for item in report.recommendations)
    assert report.selection_impact["confidence"] == "high"


def test_bimodal_distribution_detects_fast_and_slow_modes() -> None:
    report = analyze_variability(
        _bimodal_samples(),
        metric="runtime",
        unit="second",
        direction=Direction.MINIMIZE,
        group_label="g",
    )
    assert report.modes is not None
    assert 1.2 <= report.modes.fast_mode["center"] <= 1.25
    assert 1.75 <= report.modes.slow_mode["center"] <= 1.85
    assert 1.25 < report.modes.cutoff < 1.75
    assert report.modes.fast_mode["count"] == 10
    assert report.modes.slow_mode["count"] == 8
    assert report.stability["suspected_multimodal"] is True
    assert report.status in {"warning", "unstable"}
    labels = {run.run_id: run.label for run in report.runs}
    assert labels["fast-0"] == "fast_mode"
    assert labels["slow-0"] == "slow_mode"
    assert all(run.slow for run in report.runs if run.run_id.startswith("slow-"))


def test_outlier_runs_are_flagged_by_iqr_fence() -> None:
    values = [100, 101, 99, 100, 102, 101, 100, 300]
    report = analyze_variability(
        [_sample(value, run_id=f"r{index}") for index, value in enumerate(values)],
        metric="runtime",
        unit="millisecond",
        direction=Direction.MINIMIZE,
        group_label="g",
    )
    outlier_index = values.index(300)
    assert report.outliers["slow"] == [f"r{outlier_index}"]
    labels = {run.run_id: run.label for run in report.runs}
    assert labels[f"r{outlier_index}"] == "slow_outlier"


def test_association_clues_link_slow_mode_to_system_metrics() -> None:
    report = analyze_variability(
        _bimodal_samples(),
        metric="runtime",
        unit="second",
        direction=Direction.MINIMIZE,
        group_label="g",
    )
    clue_metrics = {clue.metric for clue in report.association_clues}
    assert {"dtlb_miss_count", "numa_migration_count", "cpu_migration_count"} <= clue_metrics
    for clue in report.association_clues:
        assert clue.direction == "elevated_in_slow"
        assert clue.correlation > 0.9
        assert clue.lift > 3
        # VGO discipline: clues are never phrased as causes.
        assert "关联线索" in clue.note
        assert "因果" in clue.note
    dtlb = next(clue for clue in report.association_clues if clue.metric == "dtlb_miss_count")
    assert dtlb.slow_mean is not None and dtlb.normal_mean is not None
    assert dtlb.slow_mean > dtlb.normal_mean * 4


def test_consequence_metrics_are_flagged_not_hidden() -> None:
    samples = [
        _sample(
            value,
            run_id=f"r{index}",
            system_metrics={"cycles": value * 1_000_000},
        )
        for index, value in enumerate([1.0, 1.0, 1.0, 1.0, 1.0, 2.0, 2.0, 2.0])
    ]
    report = analyze_variability(
        samples, metric="runtime", unit="second", direction=Direction.MINIMIZE, group_label="g"
    )
    assert any(
        clue.metric == "cycles" and clue.likely_consequence for clue in report.association_clues
    )


def test_attribution_identifies_dominant_host() -> None:
    samples = [_sample(100 + index, run_id=f"a{index}", host_id="host-a") for index in range(6)] + [
        _sample(150 + index, run_id=f"b{index}", host_id="host-b") for index in range(6)
    ]
    report = analyze_variability(
        samples, metric="runtime", unit="millisecond", direction=Direction.MINIMIZE, group_label="g"
    )
    host_entry = next(entry for entry in report.attribution if entry.dimension == "host")
    assert host_entry.group_count == 2
    assert host_entry.eta_squared is not None and host_entry.eta_squared > 0.9
    assert host_entry.dominant is True
    assert any(
        "宿主机" in item.action for item in report.recommendations if item.kind == "design_change"
    )


def test_recommendations_map_clues_to_control_experiments() -> None:
    report = analyze_variability(
        _bimodal_samples(),
        metric="runtime",
        unit="second",
        direction=Direction.MINIMIZE,
        group_label="g",
    )
    actions = " ".join(item.action for item in report.recommendations)
    assert "CPU affinity" in actions
    assert "NUMA balancing" in actions
    assert "透明大页" in actions
    control = [item for item in report.recommendations if item.kind == "control_experiment"]
    assert control, "clues must translate into control-variable A/B suggestions"


def test_maximize_direction_treats_low_values_as_slow() -> None:
    samples = [
        _sample(200 + (index % 3), run_id=f"fast-{index}") for index in range(10)
    ] + [_sample(150 + (index % 3), run_id=f"slow-{index}") for index in range(8)]
    report = analyze_variability(
        samples,
        metric="throughput",
        unit="MiB/s",
        direction=Direction.MAXIMIZE,
        group_label="g",
    )
    assert report.modes is not None
    assert report.modes.slow_mode["center"] < report.modes.fast_mode["center"]
    slow_ids = {run.run_id for run in report.runs if run.slow}
    assert all(run_id.startswith("slow-") for run_id in slow_ids)


def test_maximize_direction_reports_original_scale_stats() -> None:
    """Regression: MAXIMIZE stats must stay on the original (positive) scale."""

    values = [200, 202, 198, 201, 199, 203, 197, 200]
    report = analyze_variability(
        [_sample(value, run_id=f"r{index}") for index, value in enumerate(values)],
        metric="throughput",
        unit="MiB/s",
        direction=Direction.MAXIMIZE,
        group_label="g",
    )
    stats = report.distribution
    # Throughput is positive: the report must never show negated values.
    assert stats.mean == pytest.approx(sum(values) / len(values))
    assert stats.median == pytest.approx(200.0)
    assert stats.minimum == pytest.approx(197.0)
    assert stats.maximum == pytest.approx(203.0)
    # p95 keeps the "95% of runs are at least this good" semantics: for a
    # MAXIMIZE metric that is a low quantile, but still on the original scale.
    assert stats.p95 is not None and 197.0 <= stats.p95 <= 200.0
    assert stats.tail_mean is not None and 197.0 <= stats.tail_mean <= 200.0


def test_maximize_comparison_improvement_sign_is_correct() -> None:
    """Regression: a faster candidate must show positive improvement."""

    baseline = [_sample(value) for value in [110, 111, 112, 110, 111, 113]]
    candidate = [_sample(value) for value in [130, 131, 132, 130, 131, 133]]
    comparison = compare_distributions(
        baseline,
        candidate,
        metric="throughput",
        unit="MiB/s",
        direction=Direction.MAXIMIZE,
        baseline_label="A",
        candidate_label="B",
    )
    assert comparison.mean_improvement == pytest.approx(0.18, abs=0.01)
    assert comparison.median_improvement == pytest.approx(0.18, abs=0.01)
    assert comparison.verdict == "dominant"


def test_compare_distributions_reports_mean_tail_tradeoff() -> None:
    baseline = [_sample(value) for value in [99, 100, 100, 101, 100, 100, 101, 100]]
    candidate = [_sample(value) for value in [93, 94, 95, 94, 95, 94, 95, 120]]
    comparison = compare_distributions(
        baseline,
        candidate,
        metric="runtime",
        unit="millisecond",
        direction=Direction.MINIMIZE,
        baseline_label="配置 A",
        candidate_label="配置 B",
    )
    assert comparison.verdict == "mean_better_tail_worse"
    assert comparison.mean_improvement is not None and comparison.mean_improvement > 0.01
    assert comparison.tail_improvement is not None and comparison.tail_improvement < -0.05
    assert comparison.tail_worsened is True
    # The report must state the tradeoff instead of auto-selecting a winner.
    assert "均值" in comparison.summary and "尾部" in comparison.summary
    assert "不要仅凭均值" in comparison.recommendation


def test_compare_distributions_reports_dominance() -> None:
    baseline = [_sample(value) for value in [110, 111, 112, 110, 111, 113]]
    candidate = [_sample(value) for value in [90, 91, 92, 90, 91, 93]]
    comparison = compare_distributions(
        baseline,
        candidate,
        metric="runtime",
        unit="millisecond",
        direction=Direction.MINIMIZE,
        baseline_label="A",
        candidate_label="B",
    )
    assert comparison.verdict == "dominant"
    assert comparison.slow_run_probability["candidate"] == 0.0


def test_compare_distributions_slo_exceedance_probability() -> None:
    baseline = [_sample(value) for value in [110, 111, 112, 110, 111, 113, 110, 112]]
    candidate = [_sample(value) for value in [100, 101, 102, 100, 101, 103, 102, 104]]
    comparison = compare_distributions(
        baseline,
        candidate,
        metric="runtime",
        unit="millisecond",
        direction=Direction.MINIMIZE,
        baseline_label="A",
        candidate_label="B",
        slo_threshold=105.0,
    )
    assert comparison.slo_exceedance["baseline_exceedance"] == 1.0
    assert comparison.slo_exceedance["candidate_exceedance"] == 0.0


def test_compare_distributions_worst_host_across_hosts() -> None:
    baseline = [
        _sample(100, host_id="h1"),
        _sample(101, host_id="h1"),
        _sample(120, host_id="h2"),
        _sample(121, host_id="h2"),
        _sample(100, host_id="h1"),
        _sample(120, host_id="h2"),
    ]
    candidate = [
        _sample(98, host_id="h1"),
        _sample(99, host_id="h1"),
        _sample(100, host_id="h2"),
        _sample(101, host_id="h2"),
        _sample(98, host_id="h1"),
        _sample(100, host_id="h2"),
    ]
    comparison = compare_distributions(
        baseline,
        candidate,
        metric="runtime",
        unit="millisecond",
        direction=Direction.MINIMIZE,
        baseline_label="A",
        candidate_label="B",
    )
    assert comparison.worst_host["baseline"]["host"] == "h2"
    assert comparison.worst_host["candidate"]["host"] == "h2"
    baseline_median = comparison.worst_host["baseline"]["median"]
    candidate_median = comparison.worst_host["candidate"]["median"]
    assert baseline_median > candidate_median


def test_policy_thresholds_change_the_verdict() -> None:
    values = [_sample(100 + (index % 5) * 2, run_id=f"r{index}") for index in range(10)]
    strict = VariabilityPolicy(cv_stable=0.001, cv_unstable=0.002)
    report = analyze_variability(
        values,
        metric="runtime",
        unit="millisecond",
        direction=Direction.MINIMIZE,
        group_label="g",
        policy=strict,
    )
    assert report.status == "unstable"


# --- Service layer -----------------------------------------------------------------


def _add_observation(
    session,
    attempt_id: str,
    metric: str,
    value: float,
    unit: str,
    *,
    sample_index: int | None = None,
) -> None:
    session.add(
        ObservationRecord(
            id=f"obs-{attempt_id}-{metric}-{sample_index if sample_index is not None else 'x'}",
            attempt_id=attempt_id,
            metric=metric,
            value_number=value,
            unit=unit,
            phase="measurement",
            workload="medium",
            sample_index=sample_index,
            sample_count=1,
            statistic="sample",
            attributes_json={},
            created_at=datetime.now(UTC),
        )
    )


def _prepare_service_experiment(db_session) -> str:
    experiment = create_experiment(db_session, create_demo_request())
    start_experiment(db_session, experiment)
    db_session.flush()
    attempts = list(
        db_session.query(AttemptRecord).where(AttemptRecord.experiment_id == experiment.id)
    )
    # Grow each candidate's repeats so groups pass the minimum-sample gate.
    by_candidate: dict[str, list[AttemptRecord]] = {}
    for attempt in attempts:
        by_candidate.setdefault(attempt.evaluation_id, []).append(attempt)
    sequence_base = 9000
    for evaluation_id, group in by_candidate.items():
        existing = group[0]
        for extra in range(3):
            new_attempt = AttemptRecord(
                id=f"att-extra-{evaluation_id[:8]}-{extra}",
                experiment_id=existing.experiment_id,
                evaluation_id=evaluation_id,
                selection_load_point_id=None,
                repeat_index=len(group) + extra,
                retry_index=0,
                queue_sequence=sequence_base + extra,
                status=AttemptStatus.SUCCEEDED,
                fencing_token=0,
                idempotency_key=f"idem-{evaluation_id}-{len(group) + extra}",
                envelope_json={
                    "schemaVersion": "v1alpha1",
                    "target": {"fingerprint": {"hostname": "demo-host-1"}},
                },
                created_at=datetime.now(UTC),
                started_at=datetime.now(UTC) - timedelta(days=extra),
                completed_at=datetime.now(UTC),
            )
            db_session.add(new_attempt)
        sequence_base += 100
        for attempt in group:
            attempt.status = AttemptStatus.SUCCEEDED
            attempt.envelope_json = {
                "schemaVersion": "v1alpha1",
                "target": {"fingerprint": {"hostname": "demo-host-1"}},
            }
            attempt.started_at = datetime.now(UTC) - timedelta(days=attempt.repeat_index)
    db_session.flush()
    # Fill observations: primary objective throughput_mib_s (MAXIMIZE) + system metrics.
    for candidate_index, evaluation_id in enumerate(sorted(by_candidate)):
        all_attempts = (
            db_session.query(AttemptRecord)
            .where(AttemptRecord.evaluation_id == evaluation_id)
            .order_by(AttemptRecord.repeat_index)
            .all()
        )
        base = 200.0 if candidate_index == 0 else 195.0
        for position, attempt in enumerate(all_attempts):
            is_slow = position >= len(all_attempts) - 2
            value = base - 60.0 if is_slow else base + position
            _add_observation(db_session, attempt.id, "throughput_mib_s", value, "MiB/s")
            _add_observation(
                db_session, attempt.id, "context_switch_count", 500 + position * 5, "count"
            )
            _add_observation(
                db_session,
                attempt.id,
                "dtlb_miss_count",
                9000 if is_slow else 900,
                "count",
            )
    db_session.flush()
    return experiment.id


def test_service_builds_variability_report_from_experiment(db_session) -> None:
    experiment_id = _prepare_service_experiment(db_session)
    result = build_variability_report(db_session, experiment_id, persist=True)
    assert result["analyzer"] == "looper.variability-analyzer"
    assert result["metric"] == "throughput_mib_s"
    assert result["status"] == "available"
    assert len(result["groups"]) == 2
    group_labels = " ".join(group["groupLabel"] for group in result["groups"])
    assert "medium" in group_labels
    statuses = {group["status"] for group in result["groups"]}
    assert statuses <= {"stable", "warning", "unstable", "insufficient_evidence"}
    # The slow run carries elevated dtlb misses, which must surface as a clue.
    groups_with_clues = [g for g in result["groups"] if g["associationClues"]]
    assert groups_with_clues, "elevated dTLB misses in the slow run must produce a clue"
    assert any(
        clue["metric"] == "dtlb_miss_count"
        for group in groups_with_clues
        for clue in group["associationClues"]
    )
    # Baseline vs candidate distribution comparison must exist.
    assert len(result["comparisons"]) == 1
    comparison = result["comparisons"][0]
    assert comparison["metric"] == "throughput_mib_s"
    assert comparison["meanImprovement"] is not None
    assert comparison["slowRunProbability"]["baseline"] is not None
    # Snapshot persisted with a distinct variability policy digest.
    snapshots = (
        db_session.query(AnalysisSnapshotRecord)
        .where(AnalysisSnapshotRecord.experiment_id == experiment_id)
        .all()
    )
    assert len(snapshots) == 1
    assert snapshots[0].code_version == VARIABILITY_CODE_VERSION


def test_service_variability_snapshot_is_cached(db_session) -> None:
    experiment_id = _prepare_service_experiment(db_session)
    first = build_variability_report(db_session, experiment_id, persist=True)
    second = build_variability_report(db_session, experiment_id, persist=True)
    assert first == second
    snapshots = (
        db_session.query(AnalysisSnapshotRecord)
        .where(AnalysisSnapshotRecord.experiment_id == experiment_id)
        .all()
    )
    assert len(snapshots) == 1
