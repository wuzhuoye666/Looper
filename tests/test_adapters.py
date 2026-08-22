from __future__ import annotations

import json
from pathlib import Path

import pytest
from looper_benchmark_sdk.scenario import (
    benchbase_main,
    dcperf_main,
    normalize_benchbase_smallbank,
    normalize_dcperf_mediawiki,
)
from looper_core.adapters import AdapterError, json_path, load_and_apply_adapter
from looper_core.contracts import (
    AttemptResult,
    FrontierPointEvidence,
    GoodputPolicy,
    LoadSearchSpec,
    MetricObservation,
    TailEvidenceSpec,
)
from looper_core.scenario_adapters import (
    load_benchbase_smallbank_fixture,
    load_dcperf_mediawiki_fixture,
)
from looper_core.selection import analyze_slo_frontier, frontier_block_from_scenario_result


@pytest.mark.parametrize(
    ("directory", "expected"),
    [
        ("dcperf-benchpress", "throughput"),
        ("atrex", "objective_score"),
    ],
)
def test_result_adapter_fixtures(directory: str, expected: str) -> None:
    root = Path("adapters") / directory
    fixture = next((root / "fixture").iterdir())
    result = load_and_apply_adapter(root / "adapter.manifest.json", fixture)
    assert result["synthetic"] is True
    assert expected in {item["metric"] for item in result["metrics"]}


def test_ccl_workload_card_fixture() -> None:
    root = Path("adapters/ccl-workload-card")
    result = load_and_apply_adapter(
        root / "adapter.manifest.json", root / "fixture/workload-card.yaml"
    )
    assert result["fields"]["operation"] == "all-reduce"
    assert result["parameters"]["participant_count"]["value"] == 8
    assert result["metric_catalog"][0]["direction"] == "minimize"


def test_benchbase_smallbank_adapter_uses_committed_goodput() -> None:
    result = load_benchbase_smallbank_fixture(Path("adapters/benchbase-smallbank/fixture"))
    metrics = {item["metric"]: item for item in result["metrics"]}
    assert result["status"] == "succeeded"
    assert metrics["offered_tps"]["value"] == 2000
    assert metrics["attempted_tps"]["value"] == 2000
    assert metrics["committed_tps"]["value"] == 1950
    assert result["outcomes"] == {
        "committed": 117000,
        "abort": 2000,
        "retry": 500,
        "error": 500,
        "timeout": 0,
    }
    assert metrics["latency_p999_ms"]["sample_count"] == 20


def test_dcperf_mediawiki_adapter_excludes_failed_requests_from_capacity() -> None:
    result = load_dcperf_mediawiki_fixture(
        Path("adapters/dcperf-mediawiki/fixture/benchpress-result.json")
    )
    metrics = {item["metric"]: item for item in result["metrics"]}
    assert result["status"] == "succeeded"
    assert metrics["closed_loop_successful_rps"]["value"] == 1920
    assert metrics["wrk_rps"]["value"] == 1928
    assert metrics["latency_p99_ms"]["value"] == 84
    assert result["extensions"]["client_included_in_score"] is True


def _runtime_contract(output: Path) -> tuple[list[MetricObservation], AttemptResult]:
    observations = [
        MetricObservation.model_validate_json(line)
        for line in (output / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    result = AttemptResult.model_validate_json((output / "result.json").read_text(encoding="utf-8"))
    return observations, result


def test_benchbase_runtime_normalizer_emits_worker_contract(tmp_path: Path) -> None:
    fixture = Path("adapters/benchbase-smallbank/fixture")
    output = tmp_path / "benchbase-output"
    exit_code = normalize_benchbase_smallbank(
        summary_path=fixture / "summary.json",
        histograms_path=fixture / "transaction-histograms.json",
        latencies_path=fixture / "latency.raw.csv",
        accounting_path=fixture / "client-load-accounting.json",
        output=output,
        synthetic_fixture=True,
    )
    observations, result = _runtime_contract(output)
    metrics = {item.metric: item for item in observations}
    assert exit_code == 0
    assert result.status == "succeeded"
    assert metrics["offered_tps"].value == 2000
    assert metrics["attempted_tps"].value == 2000
    assert metrics["committed_tps"].value == 1950
    assert metrics["completed_requests"].value == 120000
    assert metrics["offered_load_achieved_ratio"].value == 1
    assert metrics["client_headroom_ratio"].value == 0.25
    assert metrics["latency_p99_ms"].sample_count == 20
    assert metrics["latency_p99_ms"].attributes["synthetic_fixture"] is True
    assert {item.path for item in result.artifacts} >= {
        "normalized-result.json",
        "latency.raw.csv",
        "transaction-histograms.json",
        "client-load-accounting.json",
        "benchmark.log",
    }
    assert all(
        (output / name).is_file()
        for name in [
            "normalized-result.json",
            "latency.raw.csv",
            "transaction-histograms.json",
            "client-load-accounting.json",
            "benchmark.log",
        ]
    )


def test_benchbase_normalizer_output_drives_frontier_gate(tmp_path: Path) -> None:
    fixture = Path("adapters/benchbase-smallbank/fixture")
    output = tmp_path / "benchbase-frontier"
    normalize_benchbase_smallbank(
        summary_path=fixture / "summary.json",
        histograms_path=fixture / "transaction-histograms.json",
        latencies_path=fixture / "latency.raw.csv",
        accounting_path=fixture / "client-load-accounting.json",
        output=output,
        synthetic_fixture=True,
    )
    normalized = json.loads((output / "normalized-result.json").read_text(encoding="utf-8"))
    blocks = [
        frontier_block_from_scenario_result(
            normalized,
            block_id=f"fixture-{index}",
            time_block_id=f"time-{index}",
        )
        for index in range(5)
    ]
    result = analyze_slo_frontier(
        [FrontierPointEvidence(offered_load=2000, blocks=blocks)],
        LoadSearchSpec(offered_load_metric="offered_tps", unit="transactions/second"),
        latency_p99_threshold=50,
        goodput=GoodputPolicy(metric="committed_tps", unit="transactions/second"),
        tail=TailEvidenceSpec(
            metric="transaction_latency",
            unit="ms",
            minimum_samples=100,
            histogram_format="raw",
        ),
    )
    assert blocks[0].latency_samples == 20
    assert result["decisions"][0]["status"] == "confirmed_fail"
    assert result["status"] == "needs_lower_bracket"
    assert result["next_offered_load"] == 1600


def test_dcperf_runtime_normalizer_propagates_tail_sample_count(tmp_path: Path) -> None:
    fixture = Path("adapters/dcperf-mediawiki/fixture/benchpress-result.json")
    output = tmp_path / "dcperf-output"
    exit_code = normalize_dcperf_mediawiki(
        result_path=fixture,
        output=output,
        synthetic_fixture=True,
    )
    observations, result = _runtime_contract(output)
    metrics = {item.metric: item for item in observations}
    assert exit_code == 0
    assert result.status == "succeeded"
    assert metrics["closed_loop_successful_rps"].value == 1920
    assert metrics["timeout_count"].value == 0
    assert metrics["timeout_ratio"].value == 0
    assert metrics["latency_p99_ms"].sample_count == 1_152_000
    assert (output / "benchpress-result.json").is_file()


def test_scenario_normalizer_cli_fails_closed_with_result_evidence(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"benchmark_name":"wrong"}', encoding="utf-8")
    output = tmp_path / "failed-output"
    output.mkdir()
    (output / "metrics.jsonl").write_text("stale\n", encoding="utf-8")
    (output / "normalized-result.json").write_text("{}\n", encoding="utf-8")
    exit_code = dcperf_main(["--result", str(malformed), "--output", str(output)])
    result = AttemptResult.model_validate_json((output / "result.json").read_text(encoding="utf-8"))
    assert exit_code == 2
    assert result.status == "failed"
    assert result.checks[0].id == "scenario-normalization"
    assert result.checks[0].passed is False
    assert not (output / "metrics.jsonl").exists()
    assert not (output / "normalized-result.json").exists()
    assert (output / "benchmark.log").is_file()


def test_normalizer_console_parsers_accept_fixture_flags(tmp_path: Path) -> None:
    benchbase = Path("adapters/benchbase-smallbank/fixture")
    assert (
        benchbase_main(
            [
                "--summary",
                str(benchbase / "summary.json"),
                "--histograms",
                str(benchbase / "transaction-histograms.json"),
                "--latencies",
                str(benchbase / "latency.raw.csv"),
                "--client-accounting",
                str(benchbase / "client-load-accounting.json"),
                "--output",
                str(tmp_path / "benchbase-cli"),
                "--synthetic-fixture",
            ]
        )
        == 0
    )


def test_json_path_fails_closed() -> None:
    with pytest.raises(AdapterError, match="missing"):
        json_path({"a": {}}, "$.a.b")
