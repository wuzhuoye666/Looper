from __future__ import annotations

import pytest
from looper_core.analysis import (
    bootstrap_improvement,
    environment_sensitivity,
    kendall_tau_b,
    pairwise_flip_rate,
    pareto_ranks,
    rank_stability,
    rank_stability_by_axes,
    reference_validity_rate,
    summarize,
    task_leverage,
)
from looper_core.contracts import Direction


def test_bootstrap_improvement_is_positive_for_lower_latency() -> None:
    result = bootstrap_improvement(
        [8.0, 8.2, 7.9, 8.1],
        [10.0, 10.2, 9.9, 10.1],
        Direction.MINIMIZE,
        confidence=0.95,
        resamples=500,
        seed=7,
    )
    assert result["estimate"] == pytest.approx(2.0 / 10.05)
    assert result["lower"] > 0


def test_tail_summary_reports_insufficient_evidence() -> None:
    summary = summarize([1, 2, 3], tail_min_samples=20)
    assert summary["median"] == 2
    assert summary["p99"] is None
    assert summary["tail_status"] == "insufficient_evidence"


def test_pareto_excludes_infeasible_and_ranks_fronts() -> None:
    points = [
        {"id": "fast", "feasible": True, "objectives": {"speed": 10, "cost": 5}},
        {"id": "cheap", "feasible": True, "objectives": {"speed": 8, "cost": 2}},
        {"id": "bad", "feasible": False, "objectives": {"speed": 100, "cost": 1}},
        {"id": "dominated", "feasible": True, "objectives": {"speed": 7, "cost": 6}},
    ]
    ranks = pareto_ranks(points, {"speed": "maximize", "cost": "minimize"})
    assert ranks["fast"] == 1
    assert ranks["cheap"] == 1
    assert ranks["dominated"] == 2
    assert ranks["bad"] is None


# --- Reference validity -------------------------------------------------------


def _environment(
    environment_id: str,
    *,
    eligible: bool = True,
    valid: bool | None = True,
    excluded_reason: str | None = None,
    invalid_reason: str | None = None,
) -> dict:
    return {
        "environment_id": environment_id,
        "environment_fingerprint": {"processor": environment_id},
        "eligible": eligible,
        "excluded_reason": excluded_reason,
        "valid": valid,
        "invalid_reason": invalid_reason,
        "reference_value": 12.0,
        "baseline_value": 10.0,
        "benefit": 0.2,
        "benefit_lower": 0.08,
        "benefit_upper": 0.32,
        "repeat_count": 3,
    }


def test_reference_validity_three_of_four_eligible() -> None:
    result = reference_validity_rate(
        [
            _environment("env-1"),
            _environment("env-2"),
            _environment("env-3", valid=False, invalid_reason="方向不一致"),
            _environment("env-4"),
        ],
        expected_direction="maximize",
        minimum_effect=0.05,
    )
    assert result["status"] == "partial"
    assert result["valid_environment_count"] == 3
    assert result["eligible_environment_count"] == 4
    assert result["rate"] == pytest.approx(3 / 4)
    assert result["confidence_interval"][0] < 0.75 < result["confidence_interval"][1]


def test_reference_validity_excludes_missing_reference_with_reason() -> None:
    result = reference_validity_rate(
        [
            _environment("env-1"),
            _environment("env-2"),
            {
                **_environment("env-3"),
                "eligible": False,
                "excluded_reason": "缺少 Reference 结果",
            },
        ],
        expected_direction="maximize",
        minimum_effect=0.05,
    )
    assert result["eligible_environment_count"] == 2
    assert result["excluded_environment_count"] == 1
    excluded = next(
        item for item in result["environment_results"] if item["environment_id"] == "env-3"
    )
    assert excluded["excluded_reason"] == "缺少 Reference 结果"


def test_reference_validity_single_environment_is_insufficient() -> None:
    result = reference_validity_rate(
        [_environment("env-1")],
        expected_direction="maximize",
        minimum_effect=0.05,
    )
    assert result["status"] == "insufficient_evidence"
    assert result["eligible_environment_count"] == 1


def test_reference_validity_gate_failure_is_not_direction_valid() -> None:
    result = reference_validity_rate(
        [
            # Benefit direction is fine, but the validity gate failed.
            {
                **_environment("env-1"),
                "valid": False,
                "invalid_reason": "有效性/正确性门禁未通过",
            },
            _environment("env-2"),
            _environment("env-3"),
        ],
        expected_direction="maximize",
        minimum_effect=0.05,
    )
    assert result["valid_environment_count"] == 2
    invalid = next(
        item for item in result["environment_results"] if item["environment_id"] == "env-1"
    )
    assert invalid["valid"] is False
    assert "门禁" in invalid["invalid_reason"]


def test_reference_validity_no_environments_is_unavailable() -> None:
    result = reference_validity_rate([], expected_direction="maximize", minimum_effect=0.05)
    assert result["status"] == "unavailable"
    assert result["rate"] is None


# --- Rank stability ------------------------------------------------------------


def test_kendall_tau_b_identical_rankings() -> None:
    assert kendall_tau_b([["a"], ["b"], ["c"]], [["a"], ["b"], ["c"]]) == pytest.approx(1.0)


def test_kendall_tau_b_reversed_rankings() -> None:
    assert kendall_tau_b([["a"], ["b"], ["c"]], [["c"], ["b"], ["a"]]) == pytest.approx(-1.0)


def test_kendall_tau_b_handles_ties_without_id_tiebreak() -> None:
    # First ranking ties a and b; the tie must not be resolved by item order.
    tau = kendall_tau_b([["a", "b"], ["c"]], [["a"], ["b"], ["c"]])
    assert tau is not None
    assert -1.0 <= tau <= 1.0


def test_pairwise_flip_rate_reports_flips() -> None:
    assert pairwise_flip_rate([["a"], ["b"], ["c"]], [["a"], ["b"], ["c"]]) == 0.0
    assert pairwise_flip_rate([["a"], ["b"], ["c"]], [["c"], ["b"], ["a"]]) == 1.0


def test_rank_stability_identical_slices() -> None:
    result = rank_stability([[["a"], ["b"], ["c"]], [["a"], ["b"], ["c"]]])
    assert result["comparison_count"] == 1
    assert result["median_tau"] == pytest.approx(1.0)
    assert result["pairwise_flip_rate"] == pytest.approx(0.0)


def test_rank_stability_reversed_slices() -> None:
    result = rank_stability([[["a"], ["b"], ["c"]], [["c"], ["b"], ["a"]]])
    assert result["median_tau"] == pytest.approx(-1.0)
    assert result["pairwise_flip_rate"] == pytest.approx(1.0)


def test_rank_stability_counts_ties() -> None:
    result = rank_stability([[["a", "b"], ["c"]], [["a"], ["b"], ["c"]]])
    assert result["comparison_count"] == 1
    assert result["candidate_count"] == 3
    assert result["tie_count"] == 1


def test_rank_stability_different_candidate_sets_compare_only_common() -> None:
    # Only a and b are common; c and x are ignored rather than forced into a tie.
    result = rank_stability([[["a"], ["b"], ["c"]], [["a"], ["b"], ["x"]]])
    assert result["comparison_count"] == 1
    assert result["median_tau"] == pytest.approx(1.0)


def test_rank_stability_single_slice_is_insufficient() -> None:
    result = rank_stability([[["a"], ["b"], ["c"]]])
    assert result["comparison_count"] == 0
    assert result["median_tau"] is None


def test_rank_stability_by_axes_reports_each_axis_separately() -> None:
    identical = [[["a"], ["b"], ["c"]], [["a"], ["b"], ["c"]]]
    reversed_ = [[["a"], ["b"], ["c"]], [["c"], ["b"], ["a"]]]
    result = rank_stability_by_axes(
        [
            {"axis": "machine", "rankings": identical, "scoring_formula_ids": None},
            {"axis": "day", "rankings": reversed_, "scoring_formula_ids": None},
        ]
    )
    assert len(result["axes"]) == 2
    by_axis = {axis["axis"]: axis for axis in result["axes"]}
    assert by_axis["machine"]["median_tau"] == pytest.approx(1.0)
    assert by_axis["day"]["median_tau"] == pytest.approx(-1.0)


def test_rank_stability_by_axes_no_axes_is_unavailable() -> None:
    result = rank_stability_by_axes([])
    assert result["status"] == "unavailable"


# --- Task leverage -------------------------------------------------------------


def test_task_leverage_maximum_contribution_share() -> None:
    result = task_leverage(
        {"a": {"x": 100, "y": 1}, "b": {"x": 90, "y": 2}},
        decomposable=True,
    )
    assert result["status"] == "available"
    assert result["maximum_contribution_share"] == pytest.approx(190 / 193)
    assert result["dominant_task"] == "x"


def test_task_leverage_top_contributors_top_five() -> None:
    scores = {"a": {f"t{i}": i + 1 for i in range(7)}}
    result = task_leverage(scores, decomposable=True)
    assert len(result["top_contributors"]) == 5
    assert result["top_contributors"][0]["task_id"] == "t6"


def test_task_leverage_leave_one_out_rank_change() -> None:
    result = task_leverage(
        {
            "a": {"x": 10, "y": 2},
            "b": {"x": 3, "y": 30},
        },
        decomposable=True,
    )
    # Removing task y flips the winner from b to a.
    assert result["leave_one_out"]["maximum_rank_shift"] == 1
    assert result["leave_one_out"]["winner_changed"] is True


def test_task_leverage_single_task_is_insufficient() -> None:
    result = task_leverage({"a": {"only": 10}, "b": {"only": 3}}, decomposable=True)
    assert result["status"] == "insufficient_evidence"
    assert result["maximum_contribution_share"] is None


def test_task_leverage_non_decomposable_is_unavailable() -> None:
    result = task_leverage(
        {"a": {"x": 1, "y": 2}},
        decomposable=False,
    )
    assert result["status"] == "unavailable"


def test_task_leverage_absolute_contributions_near_zero_total() -> None:
    result = task_leverage(
        {"a": {"x": 10, "y": -10}, "b": {"x": -10, "y": 10}},
        decomposable=True,
    )
    assert result["status"] == "available"
    assert result["maximum_contribution_share"] == pytest.approx(0.5)


# --- Environment sensitivity ----------------------------------------------------


def test_environment_sensitivity_two_factors_synthetic() -> None:
    records = []
    for index in range(12):
        cpu = "cpu-a" if index < 6 else "cpu-b"
        value = 1.0 if cpu == "cpu-a" else 5.0
        records.append(
            {
                "value": value,
                "cpu_model": cpu,
                "host": f"host-{index % 4}",
                "workload": "w",
                "candidate": "c",
            }
        )
    result = environment_sensitivity(
        records, ["cpu_model", "host"], controls=("workload", "candidate")
    )
    assert result["status"] in {"available", "partial"}
    cpu = next(item for item in result["factors"] if item["factor"] == "cpu_model")
    assert cpu["associated_variance_ratio"] == pytest.approx(1.0, abs=0.05)


def test_environment_sensitivity_controls_workload_and_candidate() -> None:
    records = []
    for workload, base in [("w1", 0.0), ("w2", 100.0)]:
        for candidate in ["c1", "c2"]:
            for _ in range(3):
                value = base + (5.0 if candidate == "c2" else 0.0)
                records.append(
                    {
                        "value": value,
                        "workload": workload,
                        "candidate": candidate,
                        "env": workload,  # perfectly confounded with workload
                    }
                )
    result = environment_sensitivity(records, ["env"], controls=("workload", "candidate"))
    env = next(item for item in result["factors"] if item["factor"] == "env")
    assert env["associated_variance_ratio"] is not None
    # After removing the workload fixed effect the confounded factor is ~ 0.
    assert env["associated_variance_ratio"] < 0.1


def test_environment_sensitivity_imbalance_warning() -> None:
    records = [{"value": float(index), "cpu_model": "a", "workload": "w", "candidate": "c"}
               for index in range(9)]
    records.append({"value": 100.0, "cpu_model": "b", "workload": "w", "candidate": "c"})
    result = environment_sensitivity(records, ["cpu_model"], controls=("workload", "candidate"))
    assert result["status"] in {"available", "partial"}
    assert any("不平衡" in warning for warning in result["warnings"])


def test_environment_sensitivity_collinearity_warning() -> None:
    records = []
    for host, cpu in [("h1", "c1"), ("h2", "c2")]:
        for _ in range(4):
            records.append(
                {"value": 1.0, "host": host, "cpu_model": cpu, "workload": "w", "candidate": "c"}
            )
    result = environment_sensitivity(
        records, ["host", "cpu_model"], controls=("workload", "candidate")
    )
    assert any("共线" in warning for warning in result["warnings"])


def test_environment_sensitivity_insufficient_data() -> None:
    result = environment_sensitivity(
        [{"value": 1.0, "cpu_model": "a", "workload": "w", "candidate": "c"}],
        ["cpu_model"],
        controls=("workload", "candidate"),
    )
    assert result["status"] == "insufficient_evidence"


def test_environment_sensitivity_reports_association_only() -> None:
    records = [
        {
            "value": float(index % 2),
            "cpu_model": "a" if index < 3 else "b",
            "workload": "w",
            "candidate": "c",
        }
        for index in range(6)
    ]
    result = environment_sensitivity(records, ["cpu_model"], controls=("workload", "candidate"))
    assert result["association_only"] is True
