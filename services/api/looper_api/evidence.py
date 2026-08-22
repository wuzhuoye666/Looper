from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from looper_core.canonical import canonical_json, utc_now_iso
from looper_core.cas import ArtifactCorrupt, FileSystemCAS
from sqlalchemy import select
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import Session

from looper_api.models import (
    AnalysisSnapshotRecord,
    ArtifactLinkRecord,
    ArtifactRecord,
    AttemptRecord,
    CandidateRecord,
    CheckRecord,
    EvaluationRecord,
    EventRecord,
    ExperimentRecord,
    ObservationRecord,
)


class EvidenceError(ValueError):
    pass


def _record_dict(record: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in inspect(record).mapper.column_attrs:
        value = getattr(record, column.key)
        if isinstance(value, datetime):
            value = value.isoformat()
        result[column.key] = value
    return result


def _ordered(session: Session, model: Any, predicate: Any) -> list[Any]:
    primary_key = inspect(model).primary_key[0]
    return list(session.scalars(select(model).where(predicate).order_by(primary_key)))


def build_evidence_bundle(
    session: Session,
    experiment_id: str,
    cas: FileSystemCAS,
    destination: Path,
) -> dict[str, Any]:
    experiment = session.get(ExperimentRecord, experiment_id)
    if experiment is None:
        raise EvidenceError("experiment does not exist")
    candidates = _ordered(session, CandidateRecord, CandidateRecord.experiment_id == experiment_id)
    evaluations = _ordered(
        session, EvaluationRecord, EvaluationRecord.experiment_id == experiment_id
    )
    attempts = _ordered(session, AttemptRecord, AttemptRecord.experiment_id == experiment_id)
    attempt_ids = [item.id for item in attempts]
    observations = (
        _ordered(session, ObservationRecord, ObservationRecord.attempt_id.in_(attempt_ids))
        if attempt_ids
        else []
    )
    checks = (
        _ordered(session, CheckRecord, CheckRecord.attempt_id.in_(attempt_ids))
        if attempt_ids
        else []
    )
    links = (
        _ordered(session, ArtifactLinkRecord, ArtifactLinkRecord.attempt_id.in_(attempt_ids))
        if attempt_ids
        else []
    )
    analyses = _ordered(
        session,
        AnalysisSnapshotRecord,
        AnalysisSnapshotRecord.experiment_id == experiment_id,
    )
    events = _ordered(session, EventRecord, EventRecord.experiment_id == experiment_id)
    digests = sorted({item.digest for item in links})
    artifacts = [session.get(ArtifactRecord, digest) for digest in digests]
    if any(item is None for item in artifacts):
        raise EvidenceError("an artifact link references missing metadata")

    manifest = {
        "schema_version": "looper-evidence/v1alpha1",
        "generated_at": utc_now_iso(),
        "experiment": _record_dict(experiment),
        "candidates": [_record_dict(item) for item in candidates],
        "evaluations": [_record_dict(item) for item in evaluations],
        "attempts": [_record_dict(item) for item in attempts],
        "observations": [_record_dict(item) for item in observations],
        "checks": [_record_dict(item) for item in checks],
        "artifact_links": [_record_dict(item) for item in links],
        "artifacts": [_record_dict(item) for item in artifacts if item is not None],
        "analysis_snapshots": [_record_dict(item) for item in analyses],
        "events": [_record_dict(item) for item in events],
    }
    payload = canonical_json(manifest).encode("utf-8")
    manifest_hash = hashlib.sha256(payload).hexdigest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", payload)
        archive.writestr("MANIFEST.sha256", f"{manifest_hash}  manifest.json\n")
        for artifact in artifacts:
            assert artifact is not None
            stored = cas.verify(artifact.digest, expected_size=artifact.size)
            archive.write(
                stored.path,
                arcname=f"artifacts/sha256/{artifact.digest.removeprefix('sha256:')}",
            )
    return {
        "experiment_id": experiment_id,
        "path": str(destination),
        "manifest_sha256": manifest_hash,
        "artifact_count": len(artifacts),
        "attempt_count": len(attempts),
        "observation_count": len(observations),
    }


def verify_evidence_bundle(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path, "r") as archive:
        names = archive.namelist()
        for name in names:
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts:
                raise EvidenceError(f"unsafe archive member: {name}")
        try:
            payload = archive.read("manifest.json")
            checksum_line = archive.read("MANIFEST.sha256").decode("ascii").strip()
        except KeyError as error:
            raise EvidenceError("bundle is missing its manifest") from error
        expected = checksum_line.split()[0]
        actual = hashlib.sha256(payload).hexdigest()
        if not hmac_compare(expected, actual):
            raise EvidenceError("manifest checksum does not match")
        manifest = json.loads(payload)
        if manifest.get("schema_version") != "looper-evidence/v1alpha1":
            raise EvidenceError("unsupported evidence schema")
        verified = 0
        for artifact in manifest.get("artifacts", []):
            digest = str(artifact["digest"])
            member = f"artifacts/sha256/{digest.removeprefix('sha256:')}"
            try:
                blob = archive.read(member)
            except KeyError as error:
                raise EvidenceError(f"bundle is missing {digest}") from error
            if len(blob) != int(artifact["size"]):
                raise ArtifactCorrupt(f"artifact size mismatch: {digest}")
            actual_digest = f"sha256:{hashlib.sha256(blob).hexdigest()}"
            if not hmac_compare(actual_digest, digest):
                raise ArtifactCorrupt(f"artifact digest mismatch: {digest}")
            verified += 1
    return {
        "valid": True,
        "experiment_id": manifest["experiment"]["id"],
        "manifest_sha256": actual,
        "artifact_count": verified,
        "attempt_count": len(manifest.get("attempts", [])),
        "observation_count": len(manifest.get("observations", [])),
    }


def hmac_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)
