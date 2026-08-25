from __future__ import annotations

import json
import platform
from pathlib import Path

import pytest
import typer
import yaml
from looper_api.cli import _manifest_items_for_snapshot, app
from looper_core.system_opt.demo import (
    build_demo_manifest,
    build_demo_policy,
)
from looper_core.system_opt.executor import ConfigSnapshot, OperationStatus, SnapshotEntry
from looper_core.system_opt.policy import OptimizationMode
from typer.testing import CliRunner

runner = CliRunner()


def test_reconciliation_reads_only_items_bound_by_expected_snapshot() -> None:
    manifest = build_demo_manifest()
    selected = manifest.item("vm-swappiness")
    snapshot = ConfigSnapshot(
        target_id="target-1",
        entries={
            selected.id: SnapshotEntry(
                item_id=selected.id,
                target=selected.target,
                status=OperationStatus.SUCCEEDED,
                value=selected.default,
            )
        },
    )

    assert _manifest_items_for_snapshot(manifest, snapshot) == [selected]

    unknown = snapshot.model_copy(
        update={
            "entries": {
                "outside-manifest": SnapshotEntry(
                    item_id="outside-manifest",
                    target="outside",
                    status=OperationStatus.SUCCEEDED,
                    value=1,
                )
            }
        }
    )
    with pytest.raises(typer.BadParameter, match="unknown manifest items"):
        _manifest_items_for_snapshot(manifest, unknown)


def test_demo_command_writes_a_labeled_full_closed_loop(tmp_path: Path) -> None:
    output = tmp_path / "demo.json"

    result = runner.invoke(app, ["system-opt", "demo", "--output", str(output)])

    assert result.exit_code == 0, result.output
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["evidence_kind"] == "synthetic"
    assert payload["general"]["recommended_candidate_id"] is not None
    assert payload["workload"]["routed_components"] == ["cpu"]


def test_validate_and_simulated_inventory_commands(tmp_path: Path) -> None:
    manifest = build_demo_manifest()
    policy = build_demo_policy(OptimizationMode.GENERAL)
    manifest_path = tmp_path / "manifest.yaml"
    policy_path = tmp_path / "policy.yaml"
    state_path = tmp_path / "state.json"
    inventory_path = tmp_path / "inventory.json"
    manifest_path.write_text(
        yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    policy_path.write_text(
        yaml.safe_dump(policy.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    state_path.write_text(
        json.dumps({item.id: item.default for item in manifest.items}),
        encoding="utf-8",
    )

    validated = runner.invoke(
        app,
        [
            "system-opt",
            "validate",
            "--manifest",
            str(manifest_path),
            "--policy",
            str(policy_path),
        ],
    )
    inventory = runner.invoke(
        app,
        [
            "system-opt",
            "inventory",
            "--manifest",
            str(manifest_path),
            "--backend",
            "simulated",
            "--target-id",
            "synthetic-cli",
            "--initial-state",
            str(state_path),
            "--output",
            str(inventory_path),
        ],
    )

    assert validated.exit_code == 0, validated.output
    assert inventory.exit_code == 0, inventory.output
    payload = json.loads(inventory_path.read_text(encoding="utf-8"))
    assert len(payload["items"]) == 4
    assert "no deduplication" in payload["counting_basis"]


def test_state_inventory_preserves_explicit_source_assignments(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.yaml"
    source_path = tmp_path / "90-admin.conf"
    output_path = tmp_path / "state-evidence.json"
    authorized_path = tmp_path / "authorized-state-evidence.json"
    manifest_path.write_text(
        yaml.safe_dump(build_demo_manifest().model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    source_path.write_text(
        "vm.swappiness = 10\nunrelated.value = keep-me\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "system-opt",
            "state-inventory",
            "--manifest",
            str(manifest_path),
            "--target-id",
            "target-1",
            "--source",
            str(source_path),
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert [item["key"] for item in payload["assignments"]] == [
        "vm.swappiness",
        "unrelated.value",
    ]
    swappiness = next(item for item in payload["records"] if item["item_id"] == "vm-swappiness")
    assert swappiness["ownership"] == "external-writer"

    authorized = runner.invoke(
        app,
        [
            "system-opt",
            "authorize-state",
            "--state-evidence",
            str(output_path),
            "--actor-id",
            "optimizer-a",
            "--declared-by",
            "operator-a",
            "--item-id",
            "vm-swappiness",
            "--reason",
            "controlled test target",
            "--output",
            str(authorized_path),
        ],
    )

    assert authorized.exit_code == 0, authorized.output
    authorized_payload = json.loads(authorized_path.read_text(encoding="utf-8"))
    authorized_swappiness = next(
        item for item in authorized_payload["records"] if item["item_id"] == "vm-swappiness"
    )
    assert authorized_swappiness["ownership"] == "explicit-owner"
    assert authorized_swappiness["owner_id"] == "optimizer-a"
    assert len(authorized_payload["ownership_declarations"]) == 1


def test_real_manual_command_rejects_non_linux_host_before_write(tmp_path: Path) -> None:
    if platform.system().lower() == "linux":
        return
    manifest_path = tmp_path / "manifest.yaml"
    changes_path = tmp_path / "changes.json"
    manifest_path.write_text(
        yaml.safe_dump(build_demo_manifest().model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    changes_path.write_text("{}", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "system-opt",
            "manual",
            "--manifest",
            str(manifest_path),
            "--changes",
            str(changes_path),
            "--target-id",
            "target",
            "--owner-id",
            "owner",
            "--lease-root",
            str(tmp_path / "lease"),
            "--lease-ttl-seconds",
            "60",
            "--max-changes",
            "1",
            "--allow-executable",
            "read-file",
            "--writable-root",
            str(tmp_path),
            "--output",
            str(tmp_path / "out.json"),
        ],
    )

    assert result.exit_code != 0
    assert not (tmp_path / "out.json").exists()


def test_derive_pressure_gate_writes_reproducible_evidence(tmp_path: Path) -> None:
    batch_path = tmp_path / "batch.json"
    output_path = tmp_path / "gate.json"
    batch_path.write_text(
        json.dumps(
            {
                "identity": {"target": "target-1"},
                "metrics": {
                    "cpu.score": {
                        "metric_id": "cpu.score",
                        "values": [9.0, 10.0, 11.0, 10.0, 9.5, 10.5, 10.0],
                    }
                },
                "gate_values": {},
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "system-opt",
            "derive-pressure-gate",
            "--measurement-batch",
            str(batch_path),
            "--metric-id",
            "cpu.score",
            "--confidence-level",
            "0.95",
            "--bootstrap-resamples",
            "2000",
            "--random-seed",
            "20260823",
            "--target-scope",
            "one test target",
            "--portability",
            "test-only",
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["formula_id"] == "F-PROJECT-PRESSURE-CV-BOOTSTRAP-UPPER/v1alpha1"
    assert payload["acceptance_limit"] > payload["observed_value"]
