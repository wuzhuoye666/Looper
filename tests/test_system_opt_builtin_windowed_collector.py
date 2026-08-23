from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from looper_core.system_opt.collector import (
    COLLECTION_BUNDLE_MANIFEST_NAME,
    COLLECTION_BUNDLE_MEDIA_TYPE,
    BuiltinLinuxGuestCollector,
    CollectionArtifactBundleManifest,
    CollectionArtifactBundleMember,
    CollectionInputArtifact,
    ComponentCollectionPlan,
    ComponentCollectionRequest,
    ComponentCollectionScope,
)

_ENV = "sha256:" + "1" * 64


def _bundle(path: Path) -> str:
    raw = json.dumps(
        {
            "jobs": [
                {
                    "error": 0,
                    "read": {
                        "iops": 1234,
                        "io_bytes": 4096,
                        "clat_ns": {"percentile": {"99.000000": 250000}},
                    },
                }
            ]
        }
    ).encode()
    member = CollectionArtifactBundleMember(
        path="raw/fio.json",
        media_type="application/vnd.fio+json",
        size_bytes=len(raw),
        digest="sha256:" + sha256(raw).hexdigest(),
    )
    manifest = CollectionArtifactBundleManifest(members=[member])
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            COLLECTION_BUNDLE_MANIFEST_NAME,
            json.dumps(manifest.model_dump(mode="json")),
        )
        archive.writestr(member.path, raw)
    return manifest.digest


def _diskstats(reads: int) -> str:
    return f"8 0 sda {reads} 0 0 0 50 0 0 0 0 0 0 0 0 0 0\n"


def test_builtin_window_uses_actual_elapsed_and_parses_one_bundle(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    sys = tmp_path / "sys"
    proc.mkdir()
    sys.mkdir()
    (proc / "diskstats").write_text(_diskstats(100), encoding="utf-8")
    bundle_path = tmp_path / "measure.zip"
    digest = _bundle(bundle_path)
    times = iter([10.0, 12.5])
    collector = BuiltinLinuxGuestCollector(
        proc_root=proc,
        sys_root=sys,
        monotonic=lambda: next(times),
        wall_clock=lambda: datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
    )
    requested = [
        "storage.read-iops",
        "storage.read-clat-p99-us",
        "storage.success",
        "storage.reads-completed-total",
        "storage.reads-completed-delta",
        "storage.reads-completed-per-second",
    ]
    plan = ComponentCollectionPlan(
        component="storage",
        target_id="target",
        environment_digest=_ENV,
        workload_phase_id="measure",
        workload_source="fixture fio",
        collector_id=collector.collector_id,
        requested_metrics=requested,
        interval_seconds=100.0,
        scope=ComponentCollectionScope(storage_devices=["sda"]),
    )
    session = collector.begin_collection(plan)
    (proc / "diskstats").write_text(_diskstats(130), encoding="utf-8")
    request = ComponentCollectionRequest(
        **plan.model_dump(mode="python", exclude={"schema_version"}),
        input_artifacts=[
            CollectionInputArtifact(
                artifact_id="measure-bundle",
                source=str(bundle_path),
                media_type=COLLECTION_BUNDLE_MEDIA_TYPE,
                digest=digest,
            )
        ],
        gate_values={"storage.success": True},
        measurement_identity={"run": "one"},
    )

    snapshot = session.finish(request)

    assert snapshot.metrics["storage.read-iops"].value == [1234.0]
    assert snapshot.metrics["storage.read-clat-p99-us"].value == [250.0]
    assert snapshot.metrics["storage.success"].value == [1.0]
    assert snapshot.metrics["storage.reads-completed-delta"].value == 30.0
    assert snapshot.metrics["storage.reads-completed-per-second"].value == 12.0
    assert "actual monotonic window=2.5s" in snapshot.counting_basis


def test_builtin_window_cancel_is_idempotent(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    sys = tmp_path / "sys"
    proc.mkdir()
    sys.mkdir()
    (proc / "meminfo").write_text(
        "MemTotal: 100 kB\nMemAvailable: 50 kB\n", encoding="utf-8"
    )
    collector = BuiltinLinuxGuestCollector(proc_root=proc, sys_root=sys)
    plan = ComponentCollectionPlan(
        component="memory",
        target_id="target",
        environment_digest=_ENV,
        workload_phase_id="measure",
        workload_source="fixture",
        collector_id=collector.collector_id,
        requested_metrics=["memory.available-ratio"],
        interval_seconds=10.0,
        scope=ComponentCollectionScope(),
    )

    session = collector.begin_collection(plan)
    session.cancel()
    session.cancel()
