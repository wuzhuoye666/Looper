"""PTS PHPBench package contract, parser, producer boundary, and full fixture chain."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from looper_core.manifest import load_and_validate_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "benchmarks" / "phoronix-phpbench"
FIXTURE_PATH = PACKAGE_DIR / "fixtures" / "pts-result.json"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


producer = _load_module("looper_phoronix_phpbench_producer", PACKAGE_DIR / "producer.py")
normalizer = _load_module(
    "looper_phoronix_phpbench_normalizer", PACKAGE_DIR / "normalizer.py"
)
prepare = _load_module(
    "looper_phoronix_phpbench_prepare", PACKAGE_DIR / "prepare.py"
)


def _write_envelope(tmp_path: Path, **parameters: int) -> Path:
    envelope = {
        "candidate": {
            "parameters": {
                "times_to_run": 3,
                "test_timeout_minutes": 10,
                **parameters,
            }
        },
        "workload": {
            "id": "phpbench",
            "metadata": {"profile": "pts/phpbench-1.1.6"},
        },
    }
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return path


def test_manifest_is_pinned_and_managed_executable() -> None:
    document, digest = load_and_validate_manifest(PACKAGE_DIR / "benchmark.yaml")
    assert document["metadata"]["id"] == "looper.phoronix-phpbench"
    assert document["metadata"]["source"]["commit"] == (
        "f977d6e270d5eb9eebfa26d3ca62385c00a547a6"
    )
    assert document["spec"]["workloads"][0]["metadata"]["profile"] == (
        "pts/phpbench-1.1.6"
    )
    assert document["spec"]["adapter"]["protocol"] == "looper-adapter/v1"
    assert document["spec"]["adapter"]["primaryMetric"] == "phpbench_score"
    assert document["spec"]["x-extensions"]["executionStatus"] == "executable"
    provisioning = document["spec"]["runtime"]["provisioning"]
    assert provisioning["hostCapabilities"] == ["python", "local-process", "linux"]
    assert provisioning["provides"] == ["phoronix-test-suite", "php-cli", "unzip"]
    assert "prepare" in document["spec"]["runtime"]["commands"]
    assert document["spec"]["infrastructure"]["nodeGroups"][0]["count"] == {
        "minimum": 1,
        "default": 1,
        "maximum": 1,
    }
    assert digest.startswith("sha256:")


def test_dependency_lock_digest_matches_manifest() -> None:
    from looper_core.canonical import canonical_digest

    lock = json.loads((PACKAGE_DIR / "dependency-lock.json").read_text(encoding="utf-8"))
    document, _ = load_and_validate_manifest(PACKAGE_DIR / "benchmark.yaml")
    assert canonical_digest(lock) == document["spec"]["runtime"]["dependencyLockDigest"]


def test_fixture_parser_preserves_score_and_samples() -> None:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    parsed = normalizer.parse_pts_result(document)
    assert parsed.profile == "pts/phpbench-1.1.6"
    assert parsed.scale == "Score"
    assert parsed.proportion == "HIB"
    assert parsed.score == 420000.0
    assert parsed.raw_scores == (410000.0, 420000.0, 430000.0)


def test_fixture_parser_accepts_single_trial_export_without_raw_values() -> None:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    candidate = next(iter(next(iter(document["results"].values()))["results"].values()))
    candidate.pop("raw_values")
    parsed = normalizer.parse_pts_result(document)
    assert parsed.raw_scores == (420000.0,)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("identifier", "pts/phpbench-1.1.5", "profile mismatch"),
        ("scale", "Seconds", "scale mismatch"),
        ("proportion", "LIB", "direction mismatch"),
    ],
)
def test_parser_fails_closed_on_contract_drift(field: str, value: str, message: str) -> None:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    result = next(iter(document["results"].values()))
    result[field] = value
    with pytest.raises(normalizer.NormalizationError, match=message):
        normalizer.parse_pts_result(document)


def test_parser_rejects_multiple_profiles() -> None:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    first = next(iter(document["results"].values()))
    document["results"]["unexpected"] = dict(first)
    with pytest.raises(normalizer.NormalizationError, match="exactly one PTS result"):
        normalizer.parse_pts_result(document)


def test_resolve_pts_missing_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOOPER_PTS_BIN", raising=False)
    monkeypatch.delenv("LOOPER_PHP_BIN", raising=False)
    monkeypatch.setattr(producer.shutil, "which", lambda _name: None)
    with pytest.raises(producer.PhoronixError, match="was not found"):
        producer.resolve_pts_command()


def test_resolve_pts_source_checkout_uses_core_php_entrypoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "pts-source"
    launcher = checkout / "phoronix-test-suite"
    core = checkout / "pts-core" / "phoronix-test-suite.php"
    core.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    core.write_text("<?php\n", encoding="utf-8")
    php = tmp_path / "php"
    php.write_text("", encoding="utf-8")
    monkeypatch.setenv("LOOPER_PTS_BIN", str(launcher))
    monkeypatch.setenv("LOOPER_PHP_BIN", str(php))
    assert producer.resolve_pts_command() == [str(php.resolve()), str(core.resolve())]


def test_run_contract_rejects_arbitrary_profile(tmp_path: Path) -> None:
    envelope_path = _write_envelope(tmp_path)
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["workload"]["metadata"]["profile"] = "pts/fio"
    with pytest.raises(producer.PhoronixError, match="not the pinned"):
        producer.read_run_contract(envelope)


def test_producer_and_normalizer_fixture_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    envelope = _write_envelope(tmp_path)
    output = tmp_path / "output"
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(argv, capture_output, text, timeout, env):
        assert capture_output is True and text is True and timeout > 0
        calls.append((list(argv), dict(env)))
        if "result-file-to-json" in argv:
            Path(env["OUTPUT_FILE"]).write_text(
                FIXTURE_PATH.read_text(encoding="utf-8"), encoding="utf-8"
            )
        return SimpleNamespace(returncode=0, stdout="fixture stdout", stderr="")

    monkeypatch.setattr(
        producer, "resolve_pts_command", lambda: ["php", "phoronix-test-suite"]
    )
    monkeypatch.setattr(producer.subprocess, "run", fake_run)
    assert producer.main(["--envelope", str(envelope), "--output", str(output)]) == 0
    assert calls[0][0][-2:] == ["default-benchmark", "pts/phpbench-1.1.6"]
    assert calls[0][1]["FORCE_TIMES_TO_RUN"] == "3"
    assert calls[0][1]["PHP_BIN"]
    assert calls[0][1]["TEST_RESULTS_NAME"] == "looper-phpbench"
    assert "OUTPUT_FILE" not in calls[0][1]
    assert calls[1][0][-2:] == ["result-file-to-json", "looper-phpbench"]

    assert normalizer.main(
        ["--envelope", str(envelope), "--output", str(output)]
    ) == 0
    metrics = [
        json.loads(line)
        for line in (output / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {item["metric"] for item in metrics} == {
        "pts_run_ok",
        "profile_version_match",
        "phpbench_score",
        "phpbench_score_sample",
        "sample_count",
    }
    score = next(item for item in metrics if item["metric"] == "phpbench_score")
    assert score["value"] == 420000.0
    assert score["sampleCount"] == 3
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "succeeded"
    assert all(check["passed"] for check in result["checks"])


def test_normalizer_failure_emits_only_failure_flags(tmp_path: Path) -> None:
    envelope = _write_envelope(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    invalid = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    next(iter(invalid["results"].values()))["scale"] = "Seconds"
    (output / "pts-result.json").write_text(json.dumps(invalid), encoding="utf-8")
    assert normalizer.main(
        ["--envelope", str(envelope), "--output", str(output)]
    ) == 2
    metrics = [
        json.loads(line)
        for line in (output / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {item["metric"] for item in metrics} == {
        "pts_run_ok",
        "profile_version_match",
    }
    assert all(item["value"] is False for item in metrics)
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "failed"


def test_prepare_download_retries_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_download_once(url: str, destination: Path, _sha: str) -> None:
        calls.append(url)
        if len(calls) == 1:
            raise OSError("network down")
        destination.write_bytes(b"payload")

    monkeypatch.setattr(prepare, "_download_once", fake_download_once)
    monkeypatch.setattr(prepare, "DOWNLOAD_RETRY_DELAYS", (0, 0, 0))
    destination = tmp_path / "artifact.zip"
    prepare._download(prepare.PTS_URLS, destination, "irrelevant")
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert destination.read_bytes() == b"payload"


def test_prepare_download_raises_after_all_attempts_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_download_once(url: str, destination: Path, _sha: str) -> None:
        calls.append(url)
        raise OSError("boom")

    monkeypatch.setattr(prepare, "_download_once", fake_download_once)
    monkeypatch.setattr(prepare, "DOWNLOAD_RETRY_DELAYS", (0, 0, 0))
    with pytest.raises(prepare.PreparationError):
        prepare._download(prepare.PAYLOAD_URLS, tmp_path / "payload.zip", "irrelevant")
    assert len(calls) == prepare.DOWNLOAD_ATTEMPTS
