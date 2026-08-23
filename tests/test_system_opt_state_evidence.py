from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from looper_core.system_opt.demo import build_demo_manifest
from looper_core.system_opt.executor.simulated import SimulatedBackend
from looper_core.system_opt.inventory import InventoryStatus, ManifestInventoryCollector
from looper_core.system_opt.state_evidence import (
    STATE_EVIDENCE_SCHEMA,
    ConfigStateRecord,
    ConfigurationStateEvidence,
    LinuxExactAssignmentCollector,
    OwnershipDisposition,
    PersistenceDisposition,
    StateSource,
)


def _source(path: str = "/etc/sysctl.d/90-owner.conf") -> StateSource:
    return StateSource(
        kind="user-declaration",
        locator=path,
        content_sha256=hashlib.sha256(path.encode("utf-8")).hexdigest(),
        line=1,
        raw_value="10",
    )


def test_exact_assignment_collection_preserves_all_fields_and_conflicts(
    tmp_path: Path,
) -> None:
    manifest = build_demo_manifest()
    first = tmp_path / "10-first.conf"
    second = tmp_path / "20-second.conf"
    first.write_text("vm.swappiness = 10\nunrelated.same_name = 7\n", encoding="utf-8")
    second.write_text("vm.swappiness = 20\n", encoding="utf-8")

    evidence = LinuxExactAssignmentCollector().collect(
        manifest,
        target_id="target-1",
        environment_digest="sha256:" + "a" * 64,
        source_paths=[first, second],
        collected_at=datetime(2026, 8, 23, tzinfo=UTC),
    )

    assert len(evidence.assignments) == 3
    assert {assignment.key for assignment in evidence.assignments} == {
        "vm.swappiness",
        "unrelated.same_name",
    }
    record = evidence.records_by_item()["vm-swappiness"]
    assert record.persistence == PersistenceDisposition.CONFLICT
    assert record.ownership == OwnershipDisposition.CONFLICT
    assert record.persistent_value is None
    assert "no precedence inference" in evidence.counting_basis


def test_single_exact_assignment_is_external_and_blocks_automatic_write(
    tmp_path: Path,
) -> None:
    manifest = build_demo_manifest()
    source = tmp_path / "90-admin.conf"
    source.write_text("vm.swappiness = 10\n", encoding="utf-8")
    evidence = LinuxExactAssignmentCollector().collect(
        manifest,
        target_id="target-1",
        environment_digest="sha256:" + "b" * 64,
        source_paths=[source],
        collected_at=datetime(2026, 8, 23, tzinfo=UTC),
    )

    record = evidence.records_by_item()["vm-swappiness"]
    assert record.persistence == PersistenceDisposition.DECLARED
    assert record.persistent_value == 10
    assert record.ownership == OwnershipDisposition.EXTERNAL
    pinned, blocked = evidence.safety_constraints(
        manifest,
        target_id="target-1",
        actor_id="optimizer",
    )
    assert not pinned
    assert {item.id for item in manifest.items} == blocked


def test_explicit_unowned_evidence_allows_only_the_identified_item() -> None:
    manifest = build_demo_manifest()
    item = manifest.item("vm-swappiness")
    source = _source()
    evidence = ConfigurationStateEvidence(
        schema_version=STATE_EVIDENCE_SCHEMA,
        target_id="target-1",
        manifest_digest=manifest.digest,
        environment_digest="sha256:" + "c" * 64,
        collected_at=datetime(2026, 8, 23, tzinfo=UTC),
        source_scope=[source.locator],
        assignments=[],
        records=[
            ConfigStateRecord(
                item_id=item.id,
                parameter_id=item.parameter_id,
                persistence=PersistenceDisposition.UNKNOWN,
                persistent_value=None,
                ownership=OwnershipDisposition.UNOWNED,
                owner_id=None,
                pinned=False,
                sources=[source],
                reason="operator explicitly verified that no external writer owns this item",
            )
        ],
        counting_basis="one explicit record; missing manifest items remain unknown",
    )

    pinned, blocked = evidence.safety_constraints(
        manifest,
        target_id="target-1",
        actor_id="optimizer",
    )
    assert not pinned
    assert item.id not in blocked
    assert blocked == {candidate.id for candidate in manifest.items if candidate.id != item.id}


def test_state_evidence_rejects_a_different_current_environment() -> None:
    manifest = build_demo_manifest()
    evidence = ConfigurationStateEvidence(
        schema_version=STATE_EVIDENCE_SCHEMA,
        target_id="target-1",
        manifest_digest=manifest.digest,
        environment_digest="sha256:" + "c" * 64,
        collected_at=datetime(2026, 8, 23, tzinfo=UTC),
        source_scope=["/etc/sysctl.d"],
        assignments=[],
        records=[],
        counting_basis="empty explicit scope for an identity validation test",
    )

    with pytest.raises(ValueError, match="current host"):
        evidence.safety_constraints(
            manifest,
            target_id="target-1",
            actor_id="optimizer",
            environment_digest="sha256:" + "d" * 64,
        )


def test_manifest_inventory_binds_persistent_and_ownership_evidence() -> None:
    manifest = build_demo_manifest()
    item = manifest.item("vm-swappiness")
    source = _source()
    evidence = ConfigurationStateEvidence(
        schema_version=STATE_EVIDENCE_SCHEMA,
        target_id="target-1",
        manifest_digest=manifest.digest,
        environment_digest="sha256:" + "d" * 64,
        collected_at=datetime(2026, 8, 23, tzinfo=UTC),
        source_scope=[source.locator],
        assignments=[],
        records=[
            ConfigStateRecord(
                item_id=item.id,
                parameter_id=item.parameter_id,
                persistence=PersistenceDisposition.DECLARED,
                persistent_value=10,
                ownership=OwnershipDisposition.EXPLICIT,
                owner_id="admin-a",
                pinned=True,
                sources=[source],
                reason="explicit administrator declaration",
            )
        ],
        counting_basis="one explicit record; missing manifest items remain unknown",
    )
    backend = SimulatedBackend(
        {candidate.id: candidate.default for candidate in manifest.items},
        target_id="target-1",
    )

    report = ManifestInventoryCollector().collect(
        manifest,
        backend,
        fencing_token=1,
        state_evidence=evidence,
    )

    selected = next(record for record in report.items if record.item_id == item.id)
    assert selected.persistent.status == InventoryStatus.SUCCEEDED
    assert selected.persistent.value == 10
    assert selected.ownership.status == InventoryStatus.SUCCEEDED
    assert selected.ownership.value["pinned"] is True
    assert report.metadata.state_evidence_digest == evidence.digest
