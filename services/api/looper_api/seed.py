from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any

from looper_core.canonical import canonical_digest, utc_now
from looper_core.fingerprint import system_fingerprint
from looper_core.manifest import load_and_validate_manifest
from sqlalchemy import select
from sqlalchemy.orm import Session

from looper_api.models import BenchmarkRecord, ProjectRecord, TargetRecord


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def local_fingerprint() -> dict[str, Any]:
    return system_fingerprint()


def seed_system(session: Session) -> None:
    now = utc_now()
    project = session.get(ProjectRecord, "default")
    if project is None:
        session.add(
            ProjectRecord(
                id="default",
                name="Looper Lab",
                description="Local performance optimization workspace",
                created_at=now,
                updated_at=now,
            )
        )

    fingerprint = local_fingerprint()
    capabilities = [
        "python",
        "local-process",
        platform.system().lower(),
        platform.machine().lower(),
    ]
    target = session.get(TargetRecord, "local")
    snapshot = {
        "provider": "local",
        "capabilities": capabilities,
        "fingerprint": fingerprint,
    }
    snapshot_digest = canonical_digest(snapshot)
    if target is None:
        session.add(
            TargetRecord(
                id="local",
                name="Local workstation",
                provider="local",
                status="available",
                capabilities_json=capabilities,
                inventory_json={"source": "local", "pid": os.getpid()},
                fingerprint_json=fingerprint,
                snapshot_digest=snapshot_digest,
                runnable=True,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        target.status = "available"
        target.capabilities_json = capabilities
        target.inventory_json = {"source": "local", "pid": os.getpid()}
        target.fingerprint_json = fingerprint
        target.snapshot_digest = snapshot_digest
        target.updated_at = now

    manifest_paths = sorted((repository_root() / "benchmarks").glob("*/benchmark.yaml"))
    for manifest_path in manifest_paths:
        manifest, manifest_digest = load_and_validate_manifest(manifest_path)
        metadata = manifest["metadata"]
        key = f"{metadata['id']}@{metadata['version']}"
        benchmark = session.get(BenchmarkRecord, key)
        values = {
            "benchmark_id": metadata["id"],
            "version": metadata["version"],
            "name": metadata["name"],
            "description": metadata.get("description", ""),
            "license": metadata["license"],
            "manifest_digest": manifest_digest,
            "manifest_json": manifest,
            "manifest_path": str(manifest_path),
            "trusted": manifest["spec"]["trust"] == "trusted",
        }
        if benchmark is None:
            session.add(BenchmarkRecord(key=key, installed_at=now, **values))
        else:
            for field, value in values.items():
                setattr(benchmark, field, value)

    session.flush()


def get_benchmark(session: Session, benchmark_id: str, version: str) -> BenchmarkRecord | None:
    return session.scalar(
        select(BenchmarkRecord).where(
            BenchmarkRecord.benchmark_id == benchmark_id,
            BenchmarkRecord.version == version,
        )
    )
