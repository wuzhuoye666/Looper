from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tarfile
from pathlib import Path

import pytest
from looper_api.app import _normalize_create_request
from looper_api.benchmark_compatibility import target_compatibility
from looper_api.benchmark_packages import build_directory_package
from looper_api.models import BenchmarkRecord, TargetRecord
from looper_api.serialization import benchmark_view
from looper_core.canonical import canonical_digest
from looper_core.contracts import AttemptResult, MetricObservation
from looper_core.manifest import load_and_validate_manifest

PACKAGE_ROOT = Path("benchmarks/vgo-variability").resolve()


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vgo_is_seeded_as_a_real_selection_benchmark(db_session) -> None:
    record = db_session.get(BenchmarkRecord, "looper.vgo.variability@1.1.4")
    assert record is not None
    view = benchmark_view(record)
    assert view["selectionReady"] is True
    assert view["singleNodeReady"] is True
    assert view["runnable"] is True
    assert view["primaryMetric"] == "runtime_cv"
    assert view["selectionDefaults"]["repeats"] == 1
    assert {item["id"] for item in view["workloads"]} == {"matmul", "7z", "lbm", "sad"}
    parameters = record.manifest_json["spec"]["parameters"]
    assert parameters["diagnostic_scale_percent"]["default"] == 10
    assert parameters["ab_blocks"]["default"] == 5
    assert parameters["warmups"]["default"] == 1
    assert record.manifest_json["spec"]["x-extensions"]["selectionDefaults"]["repeats"] == 1
    assert view["deploymentRequirements"] == [
        "linux",
        "local-process",
        "python",
        "root",
        "ubuntu-22.04",
    ]
    target = db_session.get(TargetRecord, "local")
    assert target is not None
    target.capabilities_json = [
        *target.capabilities_json,
        "root",
        "ubuntu-22.04",
    ]
    assert target_compatibility(record.manifest_json, target) == []


def test_vgo_quick_feasibility_parameters_only_apply_to_requested_study(db_session) -> None:
    record = db_session.get(BenchmarkRecord, "looper.vgo.variability@1.1.4")
    target = db_session.get(TargetRecord, "local")
    assert record is not None and target is not None
    target.capabilities_json = sorted(set(target.capabilities_json) | {
        "root", "ubuntu-22.04", "perf", "vgo-runtime", "parboil-2.5",
        "sharp", "sharp-3.0.0", "p7zip", "tcmalloc",
    })
    target.inventory_json = {**target.inventory_json, "logical_cpus": 8, "memory_gib": 16}

    request = _normalize_create_request({
        "mode": "selection",
        "name": "VGO 7-Zip 快速可行性测试",
        "benchmarkId": record.benchmark_id,
        "benchmarkVersion": record.version,
        "targetIds": [target.id],
        "workloadIds": ["7z"],
        "selectionParameters": {
            "diagnostic_scale_percent": 1,
            "ab_blocks": 2,
            "warmups": 0,
        },
    }, db_session)

    assert request.spec.workload_ids == ["7z"]
    assert request.spec.selection_parameters == {
        "diagnostic_scale_percent": 1,
        "ab_blocks": 2,
        "warmups": 0,
    }
    defaults = record.manifest_json["spec"]["parameters"]
    assert defaults["diagnostic_scale_percent"]["default"] == 10
    assert defaults["ab_blocks"]["default"] == 5
    assert defaults["warmups"]["default"] == 1

    provisioning = record.manifest_json["spec"]["runtime"]["provisioning"]
    assert "perf" not in provisioning["hostCapabilities"]
    assert "perf" in provisioning["provides"]
    requirements = record.manifest_json["spec"]["infrastructure"]["nodeGroups"][0]["requirements"]
    assert "perf" not in requirements["capabilities"]
    assert requirements["privileges"] == ["root"]


def test_vgo_package_and_source_snapshot_are_bounded_and_locked() -> None:
    manifest, _digest = load_and_validate_manifest(PACKAGE_ROOT / "benchmark.yaml")
    assert manifest["spec"]["runtime"]["commands"]["run"]["argv"][1].endswith("/producer.py")
    archive, package_digest = build_directory_package(PACKAGE_ROOT)
    assert archive
    assert package_digest.startswith("sha256:")

    source_lock = json.loads((PACKAGE_ROOT / "source-lock.json").read_text(encoding="utf-8"))
    with tarfile.open(PACKAGE_ROOT / source_lock["archive"]["path"], "r:gz") as snapshot:
        names = {item.name for item in snapshot.getmembers() if item.isfile()}
    assert set(source_lock["files"]) == names
    assert "scripts/run_case.sh" in names
    assert "scripts/run_case.py" in names
    assert "benchmarks/matmul/benchmark.py" in names
    dependency_lock = json.loads(
        (PACKAGE_ROOT / "dependency-lock.json").read_text(encoding="utf-8")
    )
    assert manifest["spec"]["runtime"]["dependencyLockDigest"] == canonical_digest(dependency_lock)
    assert manifest["spec"]["runtime"]["provisioning"]["cacheKey"] == canonical_digest(
        dependency_lock
    )


def test_vgo_source_snapshot_passes_archive_and_per_file_verification(
    tmp_path: Path,
) -> None:
    prepare = load_module("vgo_prepare_test", PACKAGE_ROOT / "prepare.py")
    archive, source_lock = prepare.verify_source_archive(PACKAGE_ROOT)
    extracted = tmp_path / "source"
    prepare.extract_verified_source(archive, source_lock, extracted)
    assert (extracted / "scripts" / "run_case.sh").is_file()
    assert (extracted / "scripts" / "run_case.py").is_file()


def test_vgo_parboil_download_uses_verified_multi_source_failover(tmp_path: Path) -> None:
    prepare = load_module("vgo_prepare_download_test", PACKAGE_ROOT / "prepare.py")
    archive, source_lock = prepare.verify_source_archive(PACKAGE_ROOT)
    extracted = tmp_path / "source"
    prepare.extract_verified_source(archive, source_lock, extracted)
    setup = (extracted / "scripts" / "setup_ubuntu.sh").read_text(encoding="utf-8")

    assert "VGO_PARBOIL_MIRROR_BASE_URL" in setup
    assert "http://www.phoronix-test-suite.com/benchmark-files" in setup
    assert "http://filedn.com/luEeJVCCazShDlU4ibloXvu/class" in setup
    assert "https://www.phoronix-test-suite.com/benchmark-files" in setup
    assert "--connect-timeout 10" in setup
    assert "--speed-time 20" in setup
    assert "--retry-all-errors" in setup
    assert 'local partial="$target.part"' in setup
    assert "sha256sum --check --status" in setup
    assert setup.index("http://filedn.com/") < setup.index(
        "http://www.phoronix-test-suite.com/"
    )


def test_vgo_prepare_retries_transient_package_manager_lock(tmp_path: Path, monkeypatch) -> None:
    prepare = load_module("vgo_prepare_retry_test", PACKAGE_ROOT / "prepare.py")
    calls: list[list[str]] = []
    return_codes = iter((100, 0))

    def fake_run(command, **_kwargs):
        calls.append(command)
        return prepare.subprocess.CompletedProcess(command, next(return_codes))

    delays: list[float] = []
    monkeypatch.setattr(prepare.subprocess, "run", fake_run)
    monkeypatch.setattr(prepare.time, "sleep", delays.append)

    assert (
        prepare.run_stage(
            ["bash", "scripts/setup_ubuntu.sh"],
            cwd=tmp_path,
            timeout=60,
            retry_codes={100},
            max_attempts=3,
            retry_delay_seconds=2,
        )
        == 0
    )
    assert calls == [
        ["bash", "scripts/setup_ubuntu.sh"],
        ["bash", "scripts/setup_ubuntu.sh"],
    ]
    assert delays == [2]


def test_vgo_prepare_waits_for_package_manager_holder(
    monkeypatch,
) -> None:
    prepare = load_module("vgo_prepare_lock_wait_test", PACKAGE_ROOT / "prepare.py")
    checks = iter(
        (
            prepare.subprocess.CompletedProcess(
                ["fuser"], 0, stdout="3933", stderr="/var/lib/dpkg/lock-frontend:"
            ),
            prepare.subprocess.CompletedProcess(["fuser"], 1, stdout="", stderr=""),
        )
    )
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return next(checks)

    delays: list[float] = []
    monkeypatch.setattr(prepare.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(prepare.subprocess, "run", fake_run)
    monkeypatch.setattr(prepare.time, "sleep", delays.append)

    prepare.wait_for_package_manager(timeout_seconds=60, poll_seconds=3)

    assert len(commands) == 2
    assert commands[0][:3] == ["sudo", "-n", "/usr/bin/fuser"]
    assert delays == [3]


def test_vgo_producer_invokes_original_entry_point_and_keeps_other_attempts(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    producer = load_module("vgo_producer_test", PACKAGE_ROOT / "producer.py")
    source_root = tmp_path / "vgo-source"
    run_case = source_root / "scripts" / "run_case.sh"
    run_case.parent.mkdir(parents=True)
    run_case.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    gate = source_root / "data" / "metadata" / "gate.env"
    gate.parent.mkdir(parents=True)
    gate.write_text("VGO_FULL_GO=0\nVGO_PARTIAL_GO=1\n", encoding="utf-8")
    keep = source_root / "data" / "raw" / "keep" / "marker.txt"
    keep.parent.mkdir(parents=True)
    keep.write_text("keep\n", encoding="utf-8")
    envelope = {
        "attemptId": "attempt-1",
        "leaseToken": 7,
        "experimentId": "experiment-1",
        "workload": {"id": "7z", "metadata": {}},
        "candidate": {
            "parameters": {
                "diagnostic_scale_percent": 1,
                "ab_blocks": 2,
                "warmups": 0,
                "per_run_timeout_seconds": 60,
                "inter_run_delay_milliseconds": 0,
                "order_seed": 2026,
            }
        },
    }
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    output = tmp_path / "output"

    observed_commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        observed_commands.append(command)
        assert command[:2] == ["bash", str(run_case)]
        run_id = command[command.index("--experiment-id") + 1]
        phase = command[command.index("--phase") + 1]
        condition = (
            command[command.index("--condition") + 1] if "--condition" in command else "baseline"
        )
        repetitions = int(command[command.index("--repetitions") + 1])
        raw = source_root / "data" / "raw" / run_id / "7z"
        raw.mkdir(parents=True, exist_ok=True)
        stem = f"blocked_{condition}" if phase == "blocked" else phase
        csv_path = raw / f"{stem}.csv"
        csv_path.write_text(
            "benchmark,phase,condition,exit_code,correctness,timeout,app_metric,cpu_steal\n"
            + "".join(
                f"7z,{phase},{condition},0,1,0,{1 + index / 100},0\n"
                for index in range(repetitions)
            ),
            encoding="utf-8",
        )
        (raw / f"{stem}.metadata.json").write_text("{}", encoding="utf-8")
        log = source_root / "logs" / run_id / "7z" / f"{stem}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("original log\n", encoding="utf-8")
        return producer.subprocess.CompletedProcess(command, 0, "adapter stdout\n", "")

    monkeypatch.setattr(producer.subprocess, "run", fake_run)
    monkeypatch.setenv("LOOPER_VGO_ROOT", str(source_root))
    monkeypatch.setenv("LOOPER_VGO_SOURCE_DIGEST", "sha256:" + "1" * 64)
    monkeypatch.setattr(
        sys,
        "argv",
        ["producer.py", "--envelope", str(envelope_path), "--output", str(output)],
    )
    assert producer.main() == 0
    assert (output / "vgo-raw.csv").is_file()
    assert (output / "vgo-metadata.json").is_file()
    assert (output / "vgo-native.json").is_file()
    native = json.loads((output / "vgo-native.json").read_text(encoding="utf-8"))
    assert native["machineGate"] == "partial"
    assert native["schemaVersion"] == "looper.vgo-native/v2"
    assert native["vgoExperimentId"] == "looper_attempt-1_lease-7"
    assert native["requestedCounts"] == {
        "baseline": 6,
        "profile": 3,
        "mitigated": 6,
        "rollback": 3,
        "blocks": 2,
        "perConditionPerBlock": 3,
    }
    assert [item["argv"][item["argv"].index("--phase") + 1] for item in native["commands"]] == [
        "profile",
        "blocked",
        "blocked",
        "blocked",
        "blocked",
        "rollback",
    ]
    assert len(observed_commands) == 6
    progress = capsys.readouterr().out
    assert "[vgo-progress] workload=7z stage=profile" in progress
    assert "total_samples=18/18" in progress
    assert "status=completed" in progress
    assert keep.read_text(encoding="utf-8") == "keep\n"
    assert not (source_root / "data" / "raw" / "looper_attempt-1_lease-7").exists()


def test_vgo_partial_gate_allows_sad_only_with_reversible_thp(tmp_path: Path) -> None:
    producer = load_module("vgo_producer_gate_test", PACKAGE_ROOT / "producer.py")
    source_root = tmp_path / "vgo-source"
    gate = source_root / "data" / "metadata" / "gate.env"
    gate.parent.mkdir(parents=True)
    gate.write_text("VGO_FULL_GO=0\nVGO_PARTIAL_GO=1\n", encoding="utf-8")

    assert producer.require_machine_gate(source_root, "matmul") == "partial"
    assert producer.require_machine_gate(source_root, "7z") == "partial"
    assert producer.require_machine_gate(source_root, "lbm") == "partial"
    with pytest.raises(RuntimeError, match="reversibly writable THP"):
        producer.require_machine_gate(source_root, "sad")

    gate.write_text(
        "VGO_FULL_GO=0\n"
        "VGO_PARTIAL_GO=1\n"
        "VGO_THP_READABLE=1\n"
        "VGO_THP_WRITABLE=1\n",
        encoding="utf-8",
    )
    assert producer.require_machine_gate(source_root, "sad") == "partial"


def test_vgo_quick_round_and_ten_percent_reference_are_balanced() -> None:
    producer = load_module("vgo_producer_plan_test", PACKAGE_ROOT / "producer.py")

    assert producer.scaled_plan("7z", 1, 2) == {
        "baseline": 6,
        "profile": 3,
        "mitigated": 6,
        "rollback": 3,
        "blocks": 2,
        "perConditionPerBlock": 3,
    }
    quick_orders = producer.balanced_orders("7z", 2, 2026)
    assert sorted(quick_orders) == ["baseline-first", "mitigated-first"]

    assert producer.scaled_plan("matmul", 10, 5) == {
        "baseline": 30,
        "profile": 20,
        "mitigated": 30,
        "rollback": 5,
        "blocks": 5,
        "perConditionPerBlock": 6,
    }
    assert producer.scaled_plan("lbm", 10, 5) == {
        "baseline": 50,
        "profile": 20,
        "mitigated": 50,
        "rollback": 5,
        "blocks": 5,
        "perConditionPerBlock": 10,
    }
    orders = producer.balanced_orders("lbm", 5, 2026)
    assert orders == producer.balanced_orders("lbm", 5, 2026)
    assert abs(orders.count("baseline-first") - orders.count("mitigated-first")) == 1


def test_vgo_7z_uses_wall_time_instead_of_constant_avr_usage() -> None:
    normalizer = load_module("vgo_normalizer_7z_timing_test", PACKAGE_ROOT / "normalizer.py")
    rows = [
        {"app_metric": "100.000000000", "wall_time_s": "36.68"},
        {"app_metric": "100.000000000", "wall_time_s": "36.50"},
        {"app_metric": "100.000000000", "wall_time_s": "36.49"},
    ]

    values = [normalizer.measurement_value(row, "7z") for row in rows]

    assert values == [36.68, 36.5, 36.49]
    assert normalizer.describe(values)["coefficientOfVariation"] > 0
    assert normalizer.measurement_value(rows[0], "matmul") == 100.0


def test_vgo_normalizer_emits_variability_metrics_from_original_csv(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    envelope = {
        "attemptId": "attempt-1",
        "experimentId": "experiment-1",
        "workload": {"id": "matmul", "metadata": {}},
        "candidate": {"parameters": {"diagnostic_scale_percent": 10}},
    }
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    fields = [
        "benchmark",
        "phase",
        "condition",
        "exit_code",
        "correctness",
        "timeout",
        "app_metric",
        "cpu_steal",
    ]
    with (output / "vgo-raw.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        groups = [
            ("profile", "baseline", (1.0, 1.1, 0.9)),
            ("blocked", "baseline", (1.0, 1.1, 0.9, 1.05)),
            ("blocked", "mitigated", (0.95, 0.96, 0.94, 0.95)),
            ("rollback", "baseline", (1.0, 1.02, 0.98)),
        ]
        for phase, condition, runtimes in groups:
            for index, runtime in enumerate(runtimes):
                writer.writerow(
                    {
                        "benchmark": "matmul",
                        "phase": phase,
                        "condition": condition,
                        "exit_code": "0",
                        "correctness": "1",
                        "timeout": "0",
                        "app_metric": str(runtime),
                        "cpu_steal": str(index / 10),
                    }
                )
    (output / "vgo-native.json").write_text(
        json.dumps(
            {
                "schemaVersion": "looper.vgo-native/v2",
                "workload": "matmul",
                "requestedCounts": {
                    "profile": 3,
                    "baseline": 4,
                    "mitigated": 4,
                    "rollback": 3,
                    "blocks": 2,
                    "perConditionPerBlock": 2,
                },
                "parameters": {"diagnosticScalePercent": 10, "abBlocks": 2},
                "alternatingOrder": [
                    {"block": 1, "order": "baseline-first", "runsPerCondition": 2},
                    {"block": 2, "order": "mitigated-first", "runsPerCondition": 2},
                ],
                "mitigation": {"id": "tcmalloc"},
                "exitCode": 0,
                "originalEntryPoint": "scripts/run_case.sh",
                "sourceDigest": "sha256:" + "1" * 64,
            }
        ),
        encoding="utf-8",
    )
    (output / "vgo-metadata.json").write_text("{}", encoding="utf-8")
    (output / "vgo-run.log").write_text("original VGO log\n", encoding="utf-8")

    normalizer = load_module("vgo_normalizer_test", PACKAGE_ROOT / "normalizer.py")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "normalizer.py",
            "--envelope",
            str(envelope_path),
            "--output",
            str(output),
        ],
    )
    assert normalizer.main() == 0

    observations = [
        MetricObservation.model_validate_json(line)
        for line in (output / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    manifest, _digest = load_and_validate_manifest(PACKAGE_ROOT / "benchmark.yaml")
    assert {item.metric for item in observations} <= set(manifest["spec"]["metrics"])
    by_metric = {item.metric: item for item in observations if item.statistic != "sample"}
    assert by_metric["runtime_cv"].value > 0
    assert by_metric["median_runtime_seconds"].value == 1.025
    assert by_metric["optimized_runtime_cv"].value < by_metric["runtime_cv"].value
    assert by_metric["cv_reduction_ratio"].value > 0
    assert by_metric["correctness_rate"].value == 1.0
    runtime_samples = [item for item in observations if item.metric == "runtime_seconds"]
    assert len(runtime_samples) == 8
    assert [item.sample_index for item in runtime_samples] == list(range(8))
    assert [item.attributes["condition"] for item in runtime_samples] == [
        "baseline",
        "baseline",
        "baseline",
        "baseline",
        "mitigated",
        "mitigated",
        "mitigated",
        "mitigated",
    ]
    result = AttemptResult.model_validate_json((output / "result.json").read_text(encoding="utf-8"))
    assert result.status == "succeeded"
    assert all(check.passed for check in result.checks)
    diagnostics = json.loads((output / "vgo-diagnostics.json").read_text(encoding="utf-8"))
    assert diagnostics["profileParameters"]["cpu_steal"]["count"] == 3
    assert diagnostics["alternatingOrder"][1]["order"] == "mitigated-first"
