from __future__ import annotations

from pathlib import Path

import pytest
from looper_core.canonical import canonical_digest
from looper_core.system_opt.demo import build_demo_manifest
from looper_core.system_opt.domain import (
    AuthorizedDomain,
    DomainEvidence,
    DomainResolutionError,
    resolve_domain,
)
from looper_core.system_opt.executor import OperationStatus
from looper_core.system_opt.executor.runner import SubprocessCommandRunner
from looper_core.system_opt.executor.simulated import SimulatedBackend
from looper_core.system_opt.inventory import (
    EnvironmentFingerprint,
    InventoryStatus,
    LinuxDiscoveryPolicy,
    LinuxRawCollector,
    LocalToolInventoryCollector,
    ManifestInventoryCollector,
    ToolCriticality,
    ToolRequirement,
)


def _linux_fingerprint(*, virtualization: str = "unknown") -> EnvironmentFingerprint:
    return EnvironmentFingerprint(
        os_name="linux",
        kernel_release="6.8.0-test",
        architecture="x86_64",
        distribution_id="test-linux",
        distribution_version="1",
        virtualization=virtualization,
        host_identifier_sha256="0" * 64,
        host_identifier_source="unit-test fixture",
    )


def _domain_evidence(item: object, *, verified: bool = True) -> DomainEvidence:
    return DomainEvidence(
        item_id=item.id,
        domain=item.domain,
        verified=verified,
        source="unit-test target capability probe",
        evidence_digest=canonical_digest({"item": item.id, "verified": verified}),
    )


def test_dynamic_domain_requires_verified_target_and_authorization() -> None:
    item = build_demo_manifest().item("vm-swappiness")
    authorization = AuthorizedDomain(
        item_id=item.id,
        domain=item.domain,
        reason="unit-test task authorization",
    )

    with pytest.raises(DomainResolutionError, match="unverified"):
        resolve_domain(item, _domain_evidence(item, verified=False), authorization)

    resolved = resolve_domain(item, _domain_evidence(item), authorization)
    assert resolved.minimum == 10
    assert resolved.maximum == 60
    assert resolved.parameter_id == "system.vm-swappiness"


def test_manifest_inventory_reports_every_declared_item_without_deduplication() -> None:
    manifest = build_demo_manifest()
    backend = SimulatedBackend({item.id: item.default for item in manifest.items})

    report = ManifestInventoryCollector().collect(
        manifest,
        backend,
        fencing_token=1,
        environment=_linux_fingerprint(virtualization="wsl2"),
    )

    assert len(report.items) == len(manifest.items) == 4
    assert "no deduplication" in report.counting_basis
    assert all(item.current.status == InventoryStatus.SUCCEEDED for item in report.items)
    assert all(item.preflight.status == InventoryStatus.SUCCEEDED for item in report.items)
    assert all(item.persistent.status == InventoryStatus.UNAVAILABLE for item in report.items)
    assert report.metadata.collector_environment.kernel_release == "6.8.0-test"
    assert any("must not be extrapolated" in value for value in report.metadata.scope_limitations)


def test_linux_raw_collector_preserves_distinct_equal_content_files(tmp_path: Path) -> None:
    root = tmp_path / "proc" / "sys"
    root.mkdir(parents=True)
    (root / "first").write_bytes(b"1\n")
    (root / "second").write_bytes(b"1\n")

    report = LinuxRawCollector(system_name="linux", environment=_linux_fingerprint()).collect(
        LinuxDiscoveryPolicy(roots=[root], max_files=10, max_bytes_per_file=100)
    )

    assert report.complete
    assert report.enumeration_complete
    assert report.all_values_readable
    assert len(report.records) == 2
    assert report.records[0].sha256 == report.records[1].sha256
    assert report.records[0].path != report.records[1].path
    assert "no content or metadata deduplication" in report.counting_basis


def test_linux_raw_collector_distinguishes_enumeration_from_readability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "proc" / "sys"
    root.mkdir(parents=True)
    readable = root / "readable"
    unreadable = root / "unreadable"
    readable.write_bytes(b"1\n")
    unreadable.write_bytes(b"2\n")
    original_read = LinuxRawCollector._read

    def denied(selected_root: Path, path: Path, maximum: int):
        if path == unreadable:
            from looper_core.system_opt.inventory import RawConfigRecord

            return RawConfigRecord(
                root=str(selected_root),
                path=str(path),
                status=InventoryStatus.PERMISSION_DENIED,
                message="unit-test permission denial",
            )
        return original_read(selected_root, path, maximum)

    monkeypatch.setattr(LinuxRawCollector, "_read", staticmethod(denied))
    report = LinuxRawCollector(system_name="linux", environment=_linux_fingerprint()).collect(
        LinuxDiscoveryPolicy(roots=[root], max_files=10, max_bytes_per_file=100)
    )

    assert report.enumeration_complete
    assert not report.all_values_readable
    assert not report.complete


def test_subprocess_runner_enforces_executable_and_write_root(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    target = allowed_root / "setting"
    target.write_text("old", encoding="utf-8")
    runner = SubprocessCommandRunner(
        allowed_executables={"read-file", "write-file"},
        writable_file_roots=[allowed_root],
    )

    written = runner.run(["write-file", str(target), "new"], timeout_seconds=1)
    read = runner.run(["read-file", str(target)], timeout_seconds=1)
    rejected = runner.run(["not-allowed"], timeout_seconds=1)

    assert written.status == OperationStatus.SUCCEEDED
    assert read.stdout == "new"
    assert rejected.status == OperationStatus.FAILED


def test_subprocess_runner_preserves_permission_denied_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "protected"
    target.write_text("secret", encoding="utf-8")
    runner = SubprocessCommandRunner(allowed_executables={"read-file"}, writable_file_roots=[])

    def denied(*args: object, **kwargs: object) -> str:
        raise PermissionError("unit-test permission denial")

    monkeypatch.setattr(Path, "read_text", denied)
    result = runner.run(["read-file", str(target)], timeout_seconds=1)

    assert result.status == OperationStatus.PERMISSION_DENIED


def test_tool_inventory_fails_closed_only_for_explicit_critical_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    available = {"python3": "/usr/bin/python3", "direct-proc-reader": "/opt/reader"}
    monkeypatch.setattr("looper_core.system_opt.inventory.shutil.which", available.get)
    requirements = [
        ToolRequirement(
            id="runtime",
            executable="python3",
            criticality=ToolCriticality.CRITICAL,
            purpose="run the optimizer",
        ),
        ToolRequirement(
            id="sysctl-reader",
            executable="sysctl",
            alternatives=["direct-proc-reader"],
            criticality=ToolCriticality.CRITICAL,
            purpose="read kernel settings",
        ),
        ToolRequirement(
            id="pmu",
            executable="perf",
            criticality=ToolCriticality.OPTIONAL,
            purpose="optional hardware counters",
        ),
        ToolRequirement(
            id="network-workload",
            executable="iperf3",
            criticality=ToolCriticality.CRITICAL,
            purpose="selected network workload",
        ),
    ]

    report = LocalToolInventoryCollector(
        system_name="linux", environment=_linux_fingerprint()
    ).collect(requirements)

    assert not report.critical_executables_resolved
    assert report.critical_missing == ["network-workload"]
    assert "does not prove" in report.verification_scope
    fallback = next(item for item in report.items if item.requirement_id == "sysctl-reader")
    assert fallback.selected_executable == "direct-proc-reader"
    assert next(item for item in report.items if item.requirement_id == "pmu").status == (
        InventoryStatus.UNAVAILABLE
    )
