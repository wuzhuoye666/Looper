from __future__ import annotations

from pathlib import Path

from looper_api.evidence import build_evidence_bundle, verify_evidence_bundle
from looper_api.scheduler import create_demo_request, create_experiment
from looper_core.cas import FileSystemCAS


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
