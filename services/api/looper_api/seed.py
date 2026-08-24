from __future__ import annotations

from pathlib import Path

from looper_core.canonical import utc_now
from looper_core.manifest import load_and_validate_manifest
from sqlalchemy import select
from sqlalchemy.orm import Session

from looper_api.models import BenchmarkRecord, ProjectRecord


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


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

    manifest_paths = sorted((repository_root() / "benchmarks").glob("*/benchmark.yaml"))
    for manifest_path in manifest_paths:
        manifest, manifest_digest = load_and_validate_manifest(manifest_path)
        extensions = manifest["spec"].get("x-extensions", {})
        if extensions.get("bootstrapCatalog") is not True:
            # Repository packages are source artifacts, not automatically admitted
            # catalog entries. Only built-in demos/fixtures opt into bootstrap seeding;
            # every ordinary package must enter through Benchmark Registration so it
            # receives a registration identity and an explicit audit state.
            continue
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
        elif benchmark.package_digest is None:
            for field, value in values.items():
                setattr(benchmark, field, value)
        # A package imported through Registration owns its immutable manifest path
        # and digest; startup seeding must not silently replace it with source files.

    session.flush()


def get_benchmark(session: Session, benchmark_id: str, version: str) -> BenchmarkRecord | None:
    return session.scalar(
        select(BenchmarkRecord).where(
            BenchmarkRecord.benchmark_id == benchmark_id,
            BenchmarkRecord.version == version,
        )
    )
