from __future__ import annotations

from copy import deepcopy

import pytest
from looper_api.app import _normalize_create_request, benchmark_target_options
from looper_api.benchmark_compatibility import (
    BenchmarkTargetCompatibilityError,
    single_node_contract,
    target_compatibility,
)
from looper_api.models import BenchmarkRecord, TargetRecord
from looper_api.scheduler import create_experiment, start_experiment
from looper_core.canonical import canonical_digest, utc_now
from sqlalchemy import select


def _manifest(*, requirements: dict | None = None, count: dict | None = None) -> dict:
    return {
        "spec": {
            "capabilities": ["python", "local-process", "managed-tool"],
            "infrastructure": {
                "nodeGroups": [{
                    "id": "target",
                    "role": "target",
                    "count": count or {"minimum": 1, "default": 1, "maximum": 1},
                    "requirements": requirements or {},
                }],
            },
            "runtime": {
                "provisioning": {
                    "mode": "managed",
                    "hostCapabilities": ["python", "local-process", "linux"],
                    "provides": ["managed-tool"],
                },
            },
        },
    }


def _target(
    target_id: str = "target-a",
    *,
    provider: str = "external",
    capabilities: list[str] | None = None,
    fingerprint: dict | None = None,
    runnable: bool = True,
    lifecycle_status: str = "active",
) -> TargetRecord:
    facts = fingerprint or {
        "system": "Ubuntu Linux",
        "architecture": "AMD64",
        "logical_cpu_count": 8,
        "memory_bytes": 16 * 1024**3,
    }
    tags = capabilities or ["python", "local-process", "linux", "x86_64"]
    return TargetRecord(
        id=target_id,
        name=target_id,
        provider=provider,
        status="online",
        capabilities_json=tags,
        inventory_json={},
        fingerprint_json=facts,
        snapshot_digest=canonical_digest({"fingerprint": facts, "capabilities": tags}),
        runnable=runnable,
        lifecycle_status=lifecycle_status,
        created_at=utc_now(),
        updated_at=utc_now(),
    )


def test_single_node_contract_requires_one_fixed_machine() -> None:
    manifest = _manifest()
    assert single_node_contract(manifest)["id"] == "target"
    assert single_node_contract(_manifest(count={"minimum": 1, "default": 2, "maximum": 2})) is None

    multi_group = deepcopy(manifest)
    multi_group["spec"]["infrastructure"]["nodeGroups"].append(
        deepcopy(multi_group["spec"]["infrastructure"]["nodeGroups"][0])
    )
    assert single_node_contract(multi_group) is None


def test_compatibility_normalizes_aliases_and_accepts_managed_software() -> None:
    manifest = _manifest(requirements={
        "osFamilies": ["GNU/Linux"],
        "architectures": ["x64"],
        "capabilities": ["python", "local-process", "linux"],
        "cpu": {"minimumLogicalCpus": 8},
        "memory": {"minimumGiB": 16},
        "software": [{"name": "Managed Tool", "versionConstraint": ">=1"}],
    })

    assert target_compatibility(manifest, _target()) == []


@pytest.mark.parametrize(
    ("target", "requirements", "expected_code"),
    [
        (_target(runnable=False), {}, "target_not_runnable"),
        (_target(lifecycle_status="archived"), {}, "target_inactive"),
        (
            _target(fingerprint={
                "system": "Windows", "architecture": "amd64",
                "logical_cpu_count": 8, "memory_gib": 16,
            }),
            {"osFamilies": ["linux"]},
            "os_incompatible",
        ),
        (
            _target(fingerprint={
                "system": "Linux", "architecture": "arm64",
                "logical_cpu_count": 8, "memory_gib": 16,
            }),
            {"architectures": ["x86_64"]},
            "architecture_incompatible",
        ),
        (
            _target(fingerprint={
                "system": "Linux", "architecture": "amd64",
                "logical_cpu_count": 3, "memory_gib": 16,
            }),
            {"cpu": {"minimumLogicalCpus": 4}},
            "cpu_below_minimum",
        ),
        (
            _target(fingerprint={
                "system": "Linux", "architecture": "amd64",
                "logical_cpu_count": 8, "memory_gib": 7.5,
            }),
            {"memory": {"minimumGiB": 8}},
            "memory_below_minimum",
        ),
        (
            _target(),
            {"accelerators": [{"kind": "gpu", "minimumCount": 1}]},
            "accelerators_requirement_unverifiable",
        ),
    ],
)
def test_compatibility_rejects_missing_or_unverifiable_requirements(
    target: TargetRecord,
    requirements: dict,
    expected_code: str,
) -> None:
    constraints = target_compatibility(_manifest(requirements=requirements), target)
    codes = {item["code"] for item in constraints}
    assert expected_code in codes


def test_target_options_only_exposes_compatible_environments(db_session) -> None:
    benchmark = db_session.scalar(
        select(BenchmarkRecord).where(BenchmarkRecord.benchmark_id == "looper.sysbench")
    )
    assert benchmark is not None
    local = db_session.get(TargetRecord, "local")
    local.lifecycle_status = "archived"
    compatible = _target(
        "cloud:alibaba:cn-test:i-ok",
        provider="alibaba",
        capabilities=["python", "local-process", "linux", "x86_64"],
    )
    incompatible = _target(
        "cloud:tencent:ap-test:ins-bad",
        provider="tencent",
        capabilities=["python", "local-process", "windows", "x86_64"],
        fingerprint={
            "system": "Windows", "architecture": "amd64",
            "logical_cpu_count": 8, "memory_gib": 16,
        },
    )
    db_session.add_all([compatible, incompatible])
    db_session.flush()

    result = benchmark_target_options(benchmark.benchmark_id, benchmark.version, db_session)

    assert [(item["id"], item["compatibleCount"]) for item in result["environments"]] == [
        ("alibaba-ecs", 1)
    ]
    assert result["environments"][0]["targets"][0]["id"] == compatible.id
    assert any(item["code"] == "os_incompatible" for item in result["rejectedSummary"])


def test_create_rejects_multiple_or_incompatible_targets(db_session) -> None:
    benchmark = db_session.scalar(
        select(BenchmarkRecord).where(BenchmarkRecord.benchmark_id == "looper.sysbench")
    )
    assert benchmark is not None
    second = _target("target-b")
    db_session.add(second)
    db_session.flush()

    with pytest.raises(BenchmarkTargetCompatibilityError) as multiple:
        _normalize_create_request({
            "mode": "selection",
            "name": "tampered selection",
            "benchmarkId": benchmark.benchmark_id,
            "benchmarkVersion": benchmark.version,
            "targetIds": ["local", second.id],
        }, db_session)
    assert multiple.value.constraints[0]["code"] == "single_target_required"

    second.runnable = False
    with pytest.raises(BenchmarkTargetCompatibilityError) as incompatible:
        _normalize_create_request({
            "mode": "selection",
            "name": "offline selection",
            "benchmarkId": benchmark.benchmark_id,
            "benchmarkVersion": benchmark.version,
            "targetIds": [second.id],
        }, db_session)
    assert any(item["code"] == "target_not_runnable" for item in incompatible.value.constraints)
    assert incompatible.value.status_code == 422
    assert incompatible.value.code == "benchmark_target_incompatible"


def test_start_rechecks_current_target_state(db_session) -> None:
    benchmark = db_session.scalar(
        select(BenchmarkRecord).where(BenchmarkRecord.benchmark_id == "looper.sysbench")
    )
    request = _normalize_create_request({
        "mode": "selection",
        "name": "state changes after create",
        "benchmarkId": benchmark.benchmark_id,
        "benchmarkVersion": benchmark.version,
        "targetIds": ["local"],
    }, db_session)
    experiment = create_experiment(db_session, request)
    db_session.get(TargetRecord, "local").runnable = False

    with pytest.raises(BenchmarkTargetCompatibilityError) as captured:
        start_experiment(db_session, experiment)
    assert any(item["code"] == "target_not_runnable" for item in captured.value.constraints)
