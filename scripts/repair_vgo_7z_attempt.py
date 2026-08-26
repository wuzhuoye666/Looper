from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from looper_api.config import get_settings
from looper_api.database import SessionLocal
from looper_api.events import append_event
from looper_api.models import (
    ArtifactLinkRecord,
    ArtifactRecord,
    AttemptRecord,
    EvaluationRecord,
    ExperimentRecord,
    ObservationRecord,
)
from looper_core.canonical import new_id, utc_now
from looper_core.cas import FileSystemCAS
from looper_core.contracts import MetricObservation
from sqlalchemy import select

CORRECTION_ID = "vgo-7z-wall-time-v1"
REQUIRED_INPUTS = ("vgo-raw.csv", "vgo-native.json", "vgo-metadata.json")
CORRECTED_OUTPUTS = {
    "metrics.jsonl": ("metrics.vgo-7z-wall-time-corrected.jsonl", "application/x-ndjson"),
    "result.json": ("result.vgo-7z-wall-time-corrected.json", "application/json"),
    "vgo-diagnostics.json": (
        "vgo-diagnostics.vgo-7z-wall-time-corrected.json",
        "application/json",
    ),
}


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def backup_sqlite(database_path: Path) -> Path:
    backup_dir = database_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / f"looper.db.pre-{CORRECTION_ID}-{stamp}.sqlite"
    with sqlite3.connect(database_path) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    return destination


def observation_key(
    item: MetricObservation | ObservationRecord,
) -> tuple[str, str, int | None, str]:
    if isinstance(item, MetricObservation):
        return item.metric, item.phase, item.sample_index, item.statistic
    return item.metric, item.phase, item.sample_index, item.statistic


def aggregate_values(observations: list[MetricObservation]) -> dict[str, float | bool]:
    return {
        item.metric: item.value
        for item in observations
        if item.statistic != "sample"
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-normalize one legacy VGO 7-Zip attempt from its immutable raw CSV."
    )
    parser.add_argument("attempt_id")
    parser.add_argument("--apply", action="store_true", help="Commit the corrected observations")
    args = parser.parse_args()

    repository = Path(__file__).resolve().parents[1]
    settings = get_settings()
    if not settings.database_uri.startswith("sqlite:///"):
        raise RuntimeError(
            "this repair command currently supports the local SQLite deployment only"
        )
    database_path = Path(settings.database_uri.removeprefix("sqlite:///")).resolve()
    cas = FileSystemCAS(settings.artifact_dir, settings.max_artifact_bytes)

    with SessionLocal() as session:
        attempt = session.get(AttemptRecord, args.attempt_id)
        if attempt is None:
            raise RuntimeError(f"attempt not found: {args.attempt_id}")
        evaluation = session.get(EvaluationRecord, attempt.evaluation_id)
        experiment = session.get(ExperimentRecord, attempt.experiment_id)
        if evaluation is None or experiment is None:
            raise RuntimeError("attempt is missing its evaluation or experiment")
        benchmark_id = str(experiment.spec_json.get("benchmark_id") or "")
        if benchmark_id != "looper.vgo.variability" or evaluation.workload_id != "7z":
            raise RuntimeError(
                f"refusing non-VGO/7z attempt: benchmark={benchmark_id!r}, "
                f"workload={evaluation.workload_id!r}"
            )
        if not attempt.envelope_json:
            raise RuntimeError("attempt has no immutable run envelope")

        links = list(
            session.scalars(
                select(ArtifactLinkRecord).where(ArtifactLinkRecord.attempt_id == attempt.id)
            )
        )
        links_by_name = {item.name: item for item in links}
        missing = [name for name in REQUIRED_INPUTS if name not in links_by_name]
        if missing:
            raise RuntimeError(f"attempt is missing required raw artifacts: {missing}")
        input_digests = {name: links_by_name[name].digest for name in REQUIRED_INPUTS}

        existing = list(
            session.scalars(
                select(ObservationRecord).where(ObservationRecord.attempt_id == attempt.id)
            )
        )

    with tempfile.TemporaryDirectory(prefix="looper-vgo-7z-repair-") as temporary_name:
        temporary = Path(temporary_name)
        output = temporary / "output"
        output.mkdir()
        for name, digest in input_digests.items():
            stored = cas.verify(digest)
            shutil.copyfile(stored.path, output / name)
        envelope_path = temporary / "run-envelope.json"
        envelope_path.write_text(
            json.dumps(attempt.envelope_json, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        normalizer = load_module(
            "vgo_7z_repair_normalizer",
            repository / "benchmarks" / "vgo-variability" / "normalizer.py",
        )
        original_argv = sys.argv
        try:
            sys.argv = [
                "normalizer.py",
                "--envelope",
                str(envelope_path),
                "--output",
                str(output),
            ]
            if normalizer.main() != 0:
                raise RuntimeError("VGO normalizer returned a non-zero status")
        finally:
            sys.argv = original_argv

        observations = [
            MetricObservation.model_validate_json(line)
            for line in (output / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        old_keys = {observation_key(item) for item in existing}
        new_keys = {observation_key(item) for item in observations}
        if old_keys != new_keys or len(existing) != len(observations):
            raise RuntimeError(
                "corrected observation shape differs from the stored attempt; "
                f"missing={sorted(old_keys - new_keys)}, added={sorted(new_keys - old_keys)}"
            )

        aggregates = aggregate_values(observations)
        summary = {
            name: aggregates[name]
            for name in (
                "runtime_cv",
                "optimized_runtime_cv",
                "cv_reduction_ratio",
                "median_runtime_seconds",
                "optimized_median_runtime_seconds",
                "p95_runtime_seconds",
                "optimized_p95_runtime_seconds",
            )
        }
        print(json.dumps({"attemptId": attempt.id, "corrected": summary}, indent=2))
        if not args.apply:
            print("dry-run only; pass --apply to commit the correction")
            return 0

        backup_path = backup_sqlite(database_path)
        stored_outputs = {
            source_name: cas.put_file(output / source_name)
            for source_name in CORRECTED_OUTPUTS
        }

        with SessionLocal.begin() as session:
            current = session.get(AttemptRecord, args.attempt_id)
            if current is None:
                raise RuntimeError("attempt disappeared before the correction transaction")
            current_rows = list(
                session.scalars(
                    select(ObservationRecord).where(ObservationRecord.attempt_id == current.id)
                )
            )
            current_by_key = {observation_key(item): item for item in current_rows}
            if set(current_by_key) != new_keys or len(current_rows) != len(observations):
                raise RuntimeError("attempt observations changed after the dry-run validation")

            correction = {
                "id": CORRECTION_ID,
                "reason": "7-Zip Avr first column is CPU usage; use preserved wall_time_s",
                "sourceArtifact": input_digests["vgo-raw.csv"],
            }
            for item in observations:
                row = current_by_key[observation_key(item)]
                row.value_number = None if isinstance(item.value, bool) else float(item.value)
                row.value_boolean = item.value if isinstance(item.value, bool) else None
                row.unit = item.unit
                row.workload = item.workload
                row.sample_count = item.sample_count
                row.timestamp_text = item.timestamp
                row.attributes_json = {**item.attributes, "correction": correction}

            for source_name, stored in stored_outputs.items():
                linked_name, media_type = CORRECTED_OUTPUTS[source_name]
                if session.get(ArtifactRecord, stored.digest) is None:
                    session.add(
                        ArtifactRecord(
                            digest=stored.digest,
                            size=stored.size,
                            verified=True,
                            created_at=utc_now(),
                        )
                    )
                    # ArtifactLinkRecord has no ORM relationship that can teach
                    # SQLAlchemy the dependency order, so make the referenced
                    # artifact durable in this transaction before adding its link.
                    session.flush()
                link_exists = session.scalar(
                    select(ArtifactLinkRecord.id).where(
                        ArtifactLinkRecord.attempt_id == current.id,
                        ArtifactLinkRecord.digest == stored.digest,
                        ArtifactLinkRecord.role == "result",
                        ArtifactLinkRecord.name == linked_name,
                    )
                )
                if not link_exists:
                    session.add(
                        ArtifactLinkRecord(
                            id=new_id("alink"),
                            attempt_id=current.id,
                            digest=stored.digest,
                            role="result",
                            name=linked_name,
                            media_type=media_type,
                            producer=CORRECTION_ID,
                            created_at=utc_now(),
                        )
                    )

            append_event(
                session,
                experiment_id=current.experiment_id,
                event_type="attempt.metrics_corrected",
                entity_type="attempt",
                entity_id=current.id,
                idempotency_key=f"attempt.metrics_corrected:{current.id}:{CORRECTION_ID}",
                payload={
                    "correctionId": CORRECTION_ID,
                    "sourceArtifact": input_digests["vgo-raw.csv"],
                    "correctedArtifacts": {
                        CORRECTED_OUTPUTS[name][0]: stored.digest
                        for name, stored in stored_outputs.items()
                    },
                    "summary": summary,
                    "databaseBackup": str(backup_path),
                },
            )

        print(f"applied correction; database backup: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
