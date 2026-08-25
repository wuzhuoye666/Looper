"""Sysbench benchmark package: manifest contract, producer parsing, fail closed."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSBENCH_DIR = REPO_ROOT / "benchmarks" / "sysbench"
sys.path.insert(0, str(SYSBENCH_DIR))

import normalizer  # noqa: E402
import prepare as sysbench_prepare  # noqa: E402
import producer  # noqa: E402
import versioning  # noqa: E402
from looper_core.canonical import canonical_digest  # noqa: E402
from looper_core.manifest import load_and_validate_manifest  # noqa: E402

CPU_STDOUT = """\
sysbench 1.0.20 (using bundled LuaJIT 2.1.0-beta2)

Running the test with following options:
Number of threads: 4

CPU speed:
    events per second:  1122.34

General statistics:
    total time:                          10.0010s
    total number of events:              11223

Latency (ms):
         min:                                    2.94
         avg:                                    3.51
         max:                                   21.58
         95th percentile:                        4.32
         sum:                                39427.05

Threads fairness:
    events (avg/stddev):                   2805.7500/62.10
    execution time (avg/stddev):             9.8387/0.08
"""

MEMORY_STDOUT = """\
sysbench 1.0.20 (using bundled LuaJIT 2.1.0-beta2)

Running the test with following options:
Number of threads: 4

Operations performed:
   409600 operations, 100.00 MiB transferred (10001.05 MiB/sec)

General statistics:
    total time:                          10.0023s
    total number of events:              409600

Latency (ms):
         min:                                    0.02
         avg:                                    0.10
         max:                                    2.77
         95th percentile:                        0.22
         sum:                                39876.44

Throughput:
        10001.05 MiB/sec transferred

Threads fairness:
    events (avg/stddev):                  102400.0000/0.00
    execution time (avg/stddev):             9.9691/0.00
"""

SYSBENCH_11_STDOUT = """\
sysbench 1.1.0-3ceba0b

Running the test with following options:
Number of threads: 4

time elapsed: 10.0002s
events/s (eps): 8951620.9944
"""


def _write_envelope(tmp_path: Path, test: str = "cpu", **parameters) -> Path:
    envelope = {
        "candidate": {"parameters": {"threads": 4, "time": 10, **parameters}},
        "workload": {"id": test, "metadata": {"test": test, "extraArgs": []}},
    }
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return path


def _fake_run(stdout: str, returncode: int = 0):
    def run(argv, capture_output, text, timeout):
        return SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=""
        )

    return run


# --- Manifest contract --------------------------------------------------------------


def test_manifest_validates_against_schema() -> None:
    document, digest = load_and_validate_manifest(SYSBENCH_DIR / "benchmark.yaml")
    assert document["metadata"]["id"] == "looper.sysbench"
    assert document["metadata"]["version"] == "1.0.2"
    assert document["metadata"]["source"]["commit"] == (
        "ebf1c90da05dea94648165e4f149abc20c979557"
    )
    assert document["spec"]["adapter"]["primaryMetric"] == "events_per_sec"
    assert document["spec"]["adapter"]["protocol"] == "looper-adapter/v1"
    assert {item["id"] for item in document["spec"]["workloads"]} >= {
        "cpu",
        "memory",
        "thread",
        "mutex",
    }
    # Scenario section is required for UI dropdown visibility
    scenario = document["spec"]["scenario"]
    assert scenario["id"] == "microbenchmark.sysbench.suite"
    assert scenario["workload_class"] == "microbenchmark"
    assert scenario["topology"] == "single-node"
    assert scenario["primary_metric"] == "events_per_sec"
    assert any(role["id"] == "target" for role in scenario["roles"])
    assert len(scenario["slo_gates"]) >= 1
    assert "sysbench_run_ok" in document["spec"]["metrics"]
    provisioning = document["spec"]["runtime"]["provisioning"]
    assert provisioning["mode"] == "managed"
    assert provisioning["hostCapabilities"] == ["python", "local-process", "linux"]
    assert provisioning["provides"] == ["sysbench"]
    assert "prepare" in document["spec"]["runtime"]["commands"]
    run_argv = document["spec"]["runtime"]["commands"]["run"]["argv"]
    assert run_argv[-2:] == ["--cache", "{cache}"]
    lock = json.loads((SYSBENCH_DIR / "dependency-lock.json").read_text(encoding="utf-8"))
    assert canonical_digest(lock) == document["spec"]["runtime"]["dependencyLockDigest"]
    assert provisioning["cacheKey"] == document["spec"]["runtime"]["dependencyLockDigest"]
    result_sections = document["spec"]["x-extensions"]["resultPresentation"]["sections"]
    assert result_sections[0]["view"] == "sysbench-workloads"
    assert "throughput_mib_s" in result_sections[0]["metrics"]
    assert digest.startswith("sha256:")


# --- Producer parsing ---------------------------------------------------------------


def test_parse_cpu_report() -> None:
    parsed = producer.parse_sysbench_output(CPU_STDOUT)
    assert parsed["version"] == "1.0.20"
    assert parsed["eventsPerSecond"] == 1122.34
    assert parsed["totalEvents"] == 11223
    assert parsed["latencyMs"] == {
        "min": 2.94,
        "avg": 3.51,
        "max": 21.58,
        "p95": 4.32,
        "sum": 39427.05,
    }
    assert parsed["threadsFairness"]["eventsAvg"] == 2805.75
    assert "throughput" not in parsed


def test_parse_memory_report_extracts_throughput() -> None:
    parsed = producer.parse_sysbench_output(MEMORY_STDOUT)
    # Memory tests do not print "events per second"; the producer derives it
    # from total events and measured wall time.
    assert parsed["eventsPerSecond"] == pytest.approx(409600.0 / 10.0023)
    assert parsed["throughput"] == {"unit": "MiB/sec", "value": 10001.05}


def test_parse_newer_report_labels_for_diagnostics() -> None:
    parsed = producer.parse_sysbench_output(SYSBENCH_11_STDOUT)
    assert parsed["version"] == "1.1.0"
    assert parsed["totalTimeSeconds"] == 10.0002
    assert parsed["eventsPerSecond"] == 8951620.9944


@pytest.mark.parametrize(
    "output",
    [
        "sysbench 1.0.20",
        "sysbench 1.0.20-ebf1c90 (using bundled LuaJIT 2.1.0-beta2)",
    ],
)
def test_exact_pinned_version_is_accepted(output: str) -> None:
    assert versioning.require_expected_version(output) == output


@pytest.mark.parametrize(
    "output",
    ["sysbench 1.1.0", "sysbench 1.0.200", "sysbench 13.30", ""],
)
def test_non_pinned_versions_are_rejected(output: str) -> None:
    with pytest.raises(ValueError, match="expected sysbench 1.0.20"):
        versioning.require_expected_version(output)


# --- Fail closed --------------------------------------------------------------------


def test_resolve_sysbench_missing_cache_marker_fails_closed(tmp_path) -> None:
    with pytest.raises(producer.SysbenchError, match="cache marker is unavailable"):
        producer.resolve_sysbench_bin(tmp_path)


def test_resolve_sysbench_uses_only_cache_local_exact_binary(monkeypatch, tmp_path) -> None:
    binary = tmp_path / "runtime" / "sysbench-1.0.20" / "bin" / "sysbench"
    binary.parent.mkdir(parents=True)
    binary.write_text("fixture", encoding="utf-8")
    (tmp_path / "prepared.json").write_text(
        json.dumps({"schemaVersion": versioning.PREPARED_SCHEMA, "binary": str(binary)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        producer.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="sysbench 1.0.20-ebf1c90", stderr=""
        ),
    )

    assert producer.resolve_sysbench_bin(tmp_path) == str(binary.resolve())


def test_prepare_rejects_source_digest_mismatch(tmp_path) -> None:
    archive = tmp_path / "source.tar.gz"
    archive.write_bytes(b"not the pinned source")
    with pytest.raises(sysbench_prepare.PrepareError, match="digest mismatch"):
        sysbench_prepare._verify_asset(
            archive,
            {
                "bytes": archive.stat().st_size,
                "sha256": "sha256:" + "0" * 64,
            },
        )


# --- Full chain via stub ------------------------------------------------------------


def test_producer_and_normalizer_full_chain(monkeypatch, tmp_path) -> None:
    envelope = _write_envelope(tmp_path, test="cpu")
    output = tmp_path / "output"
    monkeypatch.setattr(producer, "resolve_sysbench_bin", lambda _cache: "sysbench")
    monkeypatch.setattr(
        producer.subprocess, "run", _fake_run(CPU_STDOUT, returncode=0)
    )
    assert producer.main([
        "--envelope", str(envelope), "--output", str(output), "--cache", str(tmp_path)
    ]) == 0

    raw = json.loads((output / "raw-result.json").read_text(encoding="utf-8"))
    assert raw["exitCode"] == 0
    assert raw["eventsPerSecond"] == 1122.34

    assert normalizer.main(["--envelope", str(envelope), "--output", str(output)]) == 0
    metric_lines = (output / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    emitted = {json.loads(line)["metric"] for line in metric_lines}
    assert emitted == {
        "sysbench_run_ok",
        "events_per_sec",
        "latency_avg_ms",
        "latency_p95_ms",
        "latency_max_ms",
    }
    # Verify sysbench_run_ok = 1.0 on success
    run_ok_lines = [
        json.loads(line)
        for line in metric_lines
        if json.loads(line)["metric"] == "sysbench_run_ok"
    ]
    assert len(run_ok_lines) == 1
    assert run_ok_lines[0]["value"] == 1.0
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "succeeded"
    assert result["checks"][0] == {
        "id": "sysbench-run-ok",
        "passed": True,
        "scope": "attempt",
        "kind": "correctness",
        "message": "sysbench exit 0 with a parseable events-per-second value",
        "details": {},
    }


def test_memory_chain_emits_throughput(monkeypatch, tmp_path) -> None:
    envelope = _write_envelope(tmp_path, test="memory")
    output = tmp_path / "output"
    monkeypatch.setattr(producer, "resolve_sysbench_bin", lambda _cache: "sysbench")
    monkeypatch.setattr(
        producer.subprocess, "run", _fake_run(MEMORY_STDOUT, returncode=0)
    )
    assert producer.main([
        "--envelope", str(envelope), "--output", str(output), "--cache", str(tmp_path)
    ]) == 0
    assert normalizer.main(["--envelope", str(envelope), "--output", str(output)]) == 0
    emitted = {
        json.loads(line)["metric"]
        for line in (output / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    }
    assert "throughput_mib_s" in emitted


def test_normalizer_fails_closed_on_nonzero_exit(monkeypatch, tmp_path) -> None:
    envelope = _write_envelope(tmp_path, test="cpu")
    output = tmp_path / "output"
    monkeypatch.setattr(producer, "resolve_sysbench_bin", lambda _cache: "sysbench")
    monkeypatch.setattr(
        producer.subprocess, "run", _fake_run("fatal error", returncode=1)
    )
    assert producer.main([
        "--envelope", str(envelope), "--output", str(output), "--cache", str(tmp_path)
    ]) == 1
    assert normalizer.main(["--envelope", str(envelope), "--output", str(output)]) == 2
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["checks"][0]["passed"] is False
    # A failed run must still emit sysbench_run_ok=0 for SLO evaluation,
    # but must not fabricate any performance metrics (events/s, latency, etc.).
    metric_lines = (output / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    emitted = [json.loads(line) for line in metric_lines]
    emitted_metrics = {m["metric"] for m in emitted}
    assert emitted_metrics == {"sysbench_run_ok"}
    run_ok = next(m for m in emitted if m["metric"] == "sysbench_run_ok")
    assert run_ok["value"] == 0.0
