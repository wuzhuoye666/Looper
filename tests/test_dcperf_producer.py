"""DCPerf producer compatibility with current Benchpress history output."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path


def _producer_module():
    return _load_module("looper_dcperf_producer", "producer.py")


def _load_module(name: str, filename: str):
    path = Path(__file__).resolve().parents[1] / "benchmarks" / "dcperf-mediawiki" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_finds_current_benchpress_history_result(tmp_path: Path) -> None:
    producer = _producer_module()
    history = tmp_path / "history" / producer.JOB_NAME / "run.json"
    history.parent.mkdir(parents=True)
    history.write_text(
        json.dumps(
            {
                "benchmark_name": producer.JOB_NAME,
                "machines": [{"hostname": "target"}],
                "metrics": {"Combined": {"Wrk RPS": 135.81}},
            }
        ),
        encoding="utf-8",
    )
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text(json.dumps({"metrics": {"Combined": {}}}), encoding="utf-8")

    assert producer.find_benchpress_results(tmp_path) == [history]


def test_prefers_legacy_metric_report(tmp_path: Path) -> None:
    producer = _producer_module()
    legacy = tmp_path / "run_metrics_1.json"
    legacy.write_text("{}", encoding="utf-8")
    current = tmp_path / "history" / "run.json"
    current.parent.mkdir()
    current.write_text(
        json.dumps(
            {
                "benchmark_name": producer.JOB_NAME,
                "metrics": {"Combined": {"Wrk RPS": 1}},
            }
        ),
        encoding="utf-8",
    )

    assert producer.find_benchpress_results(tmp_path) == [legacy]


def test_parses_result_report_printed_to_stdout() -> None:
    producer = _producer_module()
    report = {
        "benchmark_name": producer.JOB_NAME,
        "metrics": {"Combined": {"Wrk RPS": 155.83}},
    }
    stdout = "native prelude\nResults Report:\n" + json.dumps(report) + "\nFinished running\n"

    assert producer.parse_benchpress_stdout_result(stdout) == report
    assert producer.parse_benchpress_stdout_result("Results Report:\nnot-json") is None


def test_native_process_output_is_teed_to_terminal_and_artifact(
    tmp_path: Path, capsys: object
) -> None:
    producer = _producer_module()
    return_code = producer.run_process(
        [
            sys.executable,
            "-u",
            "-c",
            "import sys; print('native-out'); print('native-err', file=sys.stderr)",
        ],
        tmp_path,
        tmp_path,
        os.environ.copy(),
        30,
    )

    captured = capsys.readouterr()
    assert return_code == 0
    assert captured.out.endswith("native-out\n")
    assert captured.err == "native-err\n"
    assert (tmp_path / "benchpress.stdout.log").read_text(encoding="utf-8") == "native-out\n"
    assert (tmp_path / "benchpress.stderr.log").read_text(encoding="utf-8") == "native-err\n"


def test_recovers_complete_wrk_output_referenced_by_trimmed_summary(
    tmp_path: Path, capsys: object
) -> None:
    producer = _producer_module()
    native = tmp_path / "wrk-output.log"
    native.write_text("request line 1\nrequest line 2\n", encoding="utf-8")
    destination = tmp_path / "recovered.log"

    recovered = producer.recover_referenced_wrk_output(
        f"[...trimmed to last 50 lines...]\nWrk output: {native}\n",
        destination,
    )

    assert "request line 1" in recovered
    assert "request line 2" in recovered
    assert destination.read_text(encoding="utf-8") == recovered
    assert capsys.readouterr().out == recovered


def test_stdout_only_benchpress_result_completes_full_adapter_chain(
    tmp_path: Path, monkeypatch: object
) -> None:
    producer = _producer_module()
    cache = tmp_path / "cache"
    output = tmp_path / "output"
    envelope = tmp_path / "run-envelope.json"
    cli = cache / "runtime" / "dcperf" / "benchpress_cli.py"
    wrk = cache / "runtime" / "dcperf" / "benchmarks" / "oss_performance_mediawiki" / "wrk" / "wrk"
    marker = cache / "dcperf-mediawiki-ready.json"
    hhvm = tmp_path / "hhvm"
    for path in (cli, wrk, marker, hhvm):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    envelope.write_text(
        json.dumps(
            {
                "attemptId": "att-fixture",
                "candidate": {
                    "parameters": {
                        "duration_seconds": 600,
                        "timeout_seconds": 660,
                        "profile": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    native_result = {
        "benchmark_name": producer.JOB_NAME,
        "machines": [{"hostname": "fixture-target", "num_logical_cpus": "4"}],
        "metadata": {"cpu": "fixture"},
        "metrics": {
            "Combined": {
                "Wrk requests": 77317,
                "Wrk wall sec": 600.0,
                "Wrk RPS": 200.0,
                "Wrk successful requests": 77317,
                "Wrk failed requests": 0,
                "Nginx P50 time": 0.01,
                "Nginx P95 time": 0.02,
                "Nginx P99 time": 0.03,
                # Native Nginx counters may include non-wrk health/probe requests.
                "Nginx 499": 7,
            }
        },
    }
    observed_timeouts: list[int] = []

    def fake_run_process(
        _command: list[str], _cwd: Path, native: Path, _env: dict[str, str], _timeout: int
    ) -> int:
        observed_timeouts.append(_timeout)
        (native / "benchpress.stdout.log").write_text(
            "native stdout\nResults Report:\n"
            + json.dumps(native_result)
            + "\nFinished running\n",
            encoding="utf-8",
        )
        (native / "benchpress.stderr.log").write_text("", encoding="utf-8")
        return 0

    monkeypatch.setattr(producer, "HHVM_BIN", str(hhvm))
    monkeypatch.setattr(producer, "run_process", fake_run_process)
    monkeypatch.setattr(producer, "cpu_monitor", lambda _stop, samples: samples.append(96.0))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "producer.py",
            "--envelope",
            str(envelope),
            "--output",
            str(output),
            "--cache",
            str(cache),
        ],
    )
    assert producer.main() == 0
    assert observed_timeouts == [660 + producer.NATIVE_SETUP_TIMEOUT_SECONDS]
    assert (output / "benchpress-result.json").is_file()
    assert (output / "native-system-specs.json").is_file()
    enriched = json.loads((output / "native-result-enriched.json").read_text(encoding="utf-8"))
    assert enriched["looper_monitor"]["nginx_499_raw"] == 7
    assert enriched["looper_monitor"]["timeouts"] == 0
    assert json.loads((output / "native-run.json").read_text(encoding="utf-8"))["resultSource"] == (
        "stdout:Results Report"
    )

    normalizer = _load_module("looper_dcperf_normalizer", "normalizer.py")
    monkeypatch.setattr(
        sys,
        "argv",
        ["normalizer.py", "--envelope", str(envelope), "--output", str(output)],
    )
    assert normalizer.main() == 0
    normalized = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert normalized["status"] == "succeeded"
    tail_check = next(item for item in normalized["checks"] if item["id"] == "tail-sample-count")
    assert tail_check["passed"] is False

    validator = _load_module("looper_dcperf_validator", "validate.py")
    monkeypatch.setattr(sys, "argv", ["validate.py", "--output", str(output)])
    assert validator.main() == 0
