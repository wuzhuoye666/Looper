from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tarfile
from pathlib import Path

import pytest
from looper_api.benchmark_compatibility import target_compatibility
from looper_api.benchmark_packages import build_directory_package
from looper_api.models import BenchmarkRecord, TargetRecord
from looper_api.serialization import benchmark_view
from looper_core.contracts import AttemptResult, MetricObservation
from looper_core.canonical import canonical_digest
from looper_core.manifest import load_and_validate_manifest

PACKAGE_ROOT = Path("benchmarks/vgo-variability").resolve()


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vgo_is_seeded_as_a_real_selection_benchmark(db_session) -> None:
    record = db_session.get(BenchmarkRecord, "looper.vgo.variability@1.0.2")
    assert record is not None
    view = benchmark_view(record)
    assert view["selectionReady"] is True
    assert view["singleNodeReady"] is True
    assert view["runnable"] is True
    assert view["primaryMetric"] == "runtime_cv"
    assert {item["id"] for item in view["workloads"]} == {"matmul", "7z", "lbm", "sad"}
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

    provisioning = record.manifest_json["spec"]["runtime"]["provisioning"]
    assert "perf" not in provisioning["hostCapabilities"]
    assert "perf" in provisioning["provides"]
    requirements = record.manifest_json["spec"]["infrastructure"]["nodeGroups"][0][
        "requirements"
    ]
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
    assert manifest["spec"]["runtime"]["dependencyLockDigest"] == canonical_digest(
        dependency_lock
    )
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


def test_vgo_prepare_retries_transient_package_manager_lock(
    tmp_path: Path, monkeypatch
) -> None:
    prepare = load_module("vgo_prepare_retry_test", PACKAGE_ROOT / "prepare.py")
    calls: list[list[str]] = []
    return_codes = iter((100, 0))

    def fake_run(command, **_kwargs):
        calls.append(command)
        return prepare.subprocess.CompletedProcess(command, next(return_codes))

    delays: list[float] = []
    monkeypatch.setattr(prepare.subprocess, "run", fake_run)
    monkeypatch.setattr(prepare.time, "sleep", delays.append)

    assert prepare.run_stage(
        ["bash", "scripts/setup_ubuntu.sh"],
        cwd=tmp_path,
        timeout=60,
        retry_codes={100},
        max_attempts=3,
        retry_delay_seconds=2,
    ) == 0
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
    tmp_path: Path, monkeypatch
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
        "experimentId": "experiment-1",
        "workload": {"id": "7z", "metadata": {}},
        "candidate": {
            "parameters": {
                "samples_per_attempt": 3,
                "warmups": 0,
                "per_run_timeout_seconds": 60,
            }
        },
    }
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    output = tmp_path / "output"

    def fake_run(command, **_kwargs):
        assert command[:2] == ["bash", str(run_case)]
        run_id = command[command.index("--experiment-id") + 1]
        raw = source_root / "data" / "raw" / run_id / "7z"
        raw.mkdir(parents=True)
        (raw / "baseline.csv").write_text("benchmark,app_metric\n7z,1.0\n", encoding="utf-8")
        (raw / "baseline.metadata.json").write_text("{}", encoding="utf-8")
        log = source_root / "logs" / run_id / "7z" / "baseline.log"
        log.parent.mkdir(parents=True)
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
    assert keep.read_text(encoding="utf-8") == "keep\n"
    assert not (source_root / "data" / "raw" / "looper_attempt-1").exists()


def test_vgo_partial_gate_keeps_sad_hard_blocked(tmp_path: Path) -> None:
    producer = load_module("vgo_producer_gate_test", PACKAGE_ROOT / "producer.py")
    source_root = tmp_path / "vgo-source"
    gate = source_root / "data" / "metadata" / "gate.env"
    gate.parent.mkdir(parents=True)
    gate.write_text("VGO_FULL_GO=0\nVGO_PARTIAL_GO=1\n", encoding="utf-8")

    assert producer.require_machine_gate(source_root, "matmul") == "partial"
    assert producer.require_machine_gate(source_root, "7z") == "partial"
    assert producer.require_machine_gate(source_root, "lbm") == "partial"
    with pytest.raises(RuntimeError, match="SAD requires the original VGO full gate"):
        producer.require_machine_gate(source_root, "sad")


def test_vgo_normalizer_emits_variability_metrics_from_original_csv(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    envelope = {
        "attemptId": "attempt-1",
        "experimentId": "experiment-1",
        "workload": {"id": "matmul", "metadata": {}},
        "candidate": {"parameters": {"samples_per_attempt": 5}},
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
        for index, runtime in enumerate((1.0, 1.1, 0.9, 1.05, 0.95)):
            writer.writerow(
                {
                    "benchmark": "matmul",
                    "phase": "baseline",
                    "condition": "baseline",
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
                "schemaVersion": "looper.vgo-native/v1",
                "workload": "matmul",
                "samplesRequested": 5,
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
    by_metric = {item.metric: item for item in observations if item.statistic != "sample"}
    assert by_metric["runtime_cv"].value > 0
    assert by_metric["median_runtime_seconds"].value == 1.0
    assert by_metric["correctness_rate"].value == 1.0
    assert len([item for item in observations if item.metric == "runtime_seconds"]) == 5
    result = AttemptResult.model_validate_json((output / "result.json").read_text(encoding="utf-8"))
    assert result.status == "succeeded"
    assert all(check.passed for check in result.checks)
