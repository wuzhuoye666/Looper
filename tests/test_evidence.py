from __future__ import annotations

import json
import zipfile
from pathlib import Path

from looper_api.evidence import build_evidence_bundle, verify_evidence_bundle
from looper_api.models import AttemptRecord, ObservationRecord
from looper_api.scheduler import create_demo_request, create_experiment, start_experiment
from looper_core.canonical import utc_now
from looper_core.cas import FileSystemCAS
from sqlalchemy import select


def test_empty_experiment_evidence_roundtrip(db_session: object, tmp_path: Path) -> None:
    session = db_session
    experiment = create_experiment(session, create_demo_request())
    session.flush()
    cas = FileSystemCAS(tmp_path / "cas")
    bundle = tmp_path / "evidence.zip"
    summary = build_evidence_bundle(session, experiment.id, cas, bundle)
    verified = verify_evidence_bundle(bundle)
    assert summary["attempt_count"] == 0
    assert verified["valid"] is True
    assert verified["experiment_id"] == experiment.id


def test_evidence_manifest_preserves_collection_order(db_session: object, tmp_path: Path) -> None:
    session = db_session
    experiment = create_experiment(session, create_demo_request())
    start_experiment(session, experiment)
    attempts = list(
        session.scalars(
            select(AttemptRecord)
            .where(AttemptRecord.experiment_id == experiment.id)
            .order_by(AttemptRecord.queue_sequence)
        )
    )
    first, second = attempts[:2]
    session.add_all(
        [
            ObservationRecord(
                id="obs-z-first",
                attempt_id=first.id,
                metric="throughput_mib_s",
                value_number=100.0,
                value_boolean=None,
                unit="MiB/s",
                phase="measurement",
                workload="corpus-small",
                sample_index=None,
                sample_count=None,
                statistic="median",
                timestamp_text=None,
                attributes_json={},
                created_at=utc_now(),
            ),
            ObservationRecord(
                id="obs-a-second",
                attempt_id=second.id,
                metric="throughput_mib_s",
                value_number=200.0,
                value_boolean=None,
                unit="MiB/s",
                phase="measurement",
                workload="corpus-small",
                sample_index=None,
                sample_count=None,
                statistic="median",
                timestamp_text=None,
                attributes_json={},
                created_at=utc_now(),
            ),
        ]
    )
    session.flush()

    bundle = tmp_path / "ordered-evidence.zip"
    build_evidence_bundle(session, experiment.id, FileSystemCAS(tmp_path / "cas"), bundle)
    with zipfile.ZipFile(bundle) as archive:
        manifest = json.loads(archive.read("manifest.json"))

    assert [item["queue_sequence"] for item in manifest["attempts"]] == sorted(
        item["queue_sequence"] for item in manifest["attempts"]
    )
    assert [item["attempt_id"] for item in manifest["observations"]] == [
        first.id,
        second.id,
    ]
