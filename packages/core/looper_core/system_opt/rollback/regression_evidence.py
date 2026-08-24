"""L6c regression-recovery evidence contracts and independent replay verification.

This verifier proves only internal consistency and association integrity.  It
cannot prove evidence authenticity; authenticity requires an external
signature, manifest, or trusted anchor outside this evidence directory.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePath
from typing import Annotated, Literal

from pydantic import StringConstraints, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.rollback import RollbackRecord
from looper_core.system_opt.rollback.regression import (
    RegressionRecoveryOutcome,
    RegressionRecoveryRequest,
    RegressionRecoveryStatus,
)

REGRESSION_RECOVERY_EVIDENCE_INDEX_SCHEMA = (
    "looper.regression-recovery-evidence-index/v1alpha1"
)
REGRESSION_RECOVERY_EVIDENCE_VERIFICATION_SCHEMA = (
    "looper.regression-recovery-evidence-verification/v1alpha1"
)
REGRESSION_RECOVERY_EVIDENCE_INDEX_FILENAME = "regression-recovery-evidence-index.json"
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
Digest = Annotated[str, StringConstraints(pattern=_DIGEST_PATTERN)]
EvidenceFilename = Annotated[
    str,
    StringConstraints(pattern=r"^(request|outcome|rollback)-[0-9a-f]{64}\.json$"),
]
_KNOWN_PREFIX = re.compile(r"^(request|outcome|rollback)-")


class RegressionRecoveryEvidenceVerificationError(ValueError):
    """The L6c evidence directory failed fail-closed replay verification."""


class RegressionRecoveryEvidenceIndex(StrictModel):
    """Fixed index for one complete content-addressed L6c evidence graph."""

    schema_version: Literal[REGRESSION_RECOVERY_EVIDENCE_INDEX_SCHEMA] = (
        REGRESSION_RECOVERY_EVIDENCE_INDEX_SCHEMA
    )
    request_digest: Digest
    outcome_digest: Digest
    rollback_record_digest: Digest | None = None
    request_path: EvidenceFilename
    outcome_path: EvidenceFilename
    rollback_record_path: EvidenceFilename | None = None

    @model_validator(mode="after")
    def validate_content_addressed_paths(self) -> RegressionRecoveryEvidenceIndex:
        _require_pure_filename(self.request_path, "request")
        _require_pure_filename(self.outcome_path, "outcome")
        expected_request = evidence_filename("request", self.request_digest)
        expected_outcome = evidence_filename("outcome", self.outcome_digest)
        if self.request_path != expected_request:
            raise ValueError("request evidence path is not bound to its digest")
        if self.outcome_path != expected_outcome:
            raise ValueError("outcome evidence path is not bound to its digest")
        if (self.rollback_record_digest is None) != (self.rollback_record_path is None):
            raise ValueError("rollback digest and path must be present together")
        if self.rollback_record_digest is not None:
            assert self.rollback_record_path is not None
            _require_pure_filename(self.rollback_record_path, "rollback")
            if self.rollback_record_path != evidence_filename(
                "rollback", self.rollback_record_digest
            ):
                raise ValueError("rollback evidence path is not bound to its digest")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class RegressionRecoveryEvidenceGraph(StrictModel):
    """Complete in-memory graph validated before the fixed index is published."""

    request: RegressionRecoveryRequest
    outcome: RegressionRecoveryOutcome
    index: RegressionRecoveryEvidenceIndex

    @model_validator(mode="after")
    def validate_associations(self) -> RegressionRecoveryEvidenceGraph:
        request, outcome, index = self.request, self.outcome, self.index
        if outcome.request_digest != request.digest:
            raise ValueError("forged outcome request binding")
        if index.request_digest != request.digest:
            raise ValueError("index request digest does not match request")
        if index.outcome_digest != outcome.digest:
            raise ValueError("index outcome digest does not match outcome")
        execution = outcome.execution_evidence
        rollback = outcome.rollback_record
        if outcome.status is RegressionRecoveryStatus.NOT_TRIGGERED:
            if execution is not None or rollback is not None:
                raise ValueError("not-triggered outcome carries execution or rollback")
            if index.rollback_record_digest is not None:
                raise ValueError("not-triggered index carries rollback")
            return self
        if execution is None or rollback is None:
            raise ValueError("triggered outcome lacks execution or rollback")
        if execution.request_digest != request.digest:
            raise ValueError("forged execution request binding")
        if index.rollback_record_digest != rollback.digest:
            raise ValueError("index rollback digest does not match outcome")
        checkpoint = request.checkpoint
        if rollback.target_id != checkpoint.target_id:
            raise ValueError("forged rollback target binding")
        if rollback.item_ids != sorted(checkpoint.snapshot.entries):
            raise ValueError("forged rollback item binding")
        if rollback.baseline_snapshot_digest != checkpoint.snapshot.digest:
            raise ValueError("forged rollback baseline snapshot binding")
        if rollback.checkpoint_digest != checkpoint.digest:
            raise ValueError("forged rollback checkpoint binding")
        if rollback.regression_vector_digest != request.current_vector.digest:
            raise ValueError("forged rollback result-vector binding")
        if rollback.regression_threshold != request.regression_threshold:
            raise ValueError("forged rollback threshold binding")
        expected_refs = {request.digest, execution.digest}
        if set(rollback.evidence_digests) != expected_refs:
            raise ValueError("forged rollback request/execution evidence binding")
        safety = execution.safety_result
        final_snapshot = safety.final_snapshot if safety is not None else None
        final_digest = final_snapshot.digest if final_snapshot is not None else None
        if rollback.final_snapshot_digest != final_digest:
            raise ValueError("forged rollback final snapshot binding")
        restoration = execution.restoration
        if restoration is not None:
            if restoration.baseline_snapshot_digest != checkpoint.snapshot.digest:
                raise ValueError("forged restoration baseline binding")
            if restoration.actual_snapshot_digest != final_digest:
                raise ValueError("forged restoration actual snapshot binding")
        return self


class RegressionRecoveryEvidenceVerification(StrictModel):
    """Verified internal summary; this does not establish evidence authenticity."""

    schema_version: Literal[REGRESSION_RECOVERY_EVIDENCE_VERIFICATION_SCHEMA] = (
        REGRESSION_RECOVERY_EVIDENCE_VERIFICATION_SCHEMA
    )
    index_digest: Digest
    request_digest: Digest
    outcome_digest: Digest
    rollback_record_digest: Digest | None = None
    status: RegressionRecoveryStatus
    stop_required: bool
    rollback_present: bool

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


def evidence_filename(kind: str, digest: str) -> str:
    if kind not in {"request", "outcome", "rollback"}:
        raise ValueError(f"unsupported regression evidence kind: {kind}")
    if re.fullmatch(_DIGEST_PATTERN, digest) is None:
        raise ValueError("regression evidence digest must be canonical sha256")
    return f"{kind}-{digest.removeprefix('sha256:')}.json"


def _require_pure_filename(value: str, label: str) -> None:
    path = PurePath(value)
    if path.is_absolute() or path.name != value or ".." in path.parts or len(path.parts) != 1:
        raise ValueError(f"{label} evidence path must be a pure filename")


def build_regression_recovery_evidence_graph(
    request: RegressionRecoveryRequest,
    outcome: RegressionRecoveryOutcome,
) -> RegressionRecoveryEvidenceGraph:
    rollback = outcome.rollback_record
    rollback_digest = rollback.digest if rollback is not None else None
    return RegressionRecoveryEvidenceGraph(
        request=request,
        outcome=outcome,
        index=RegressionRecoveryEvidenceIndex(
            request_digest=request.digest,
            outcome_digest=outcome.digest,
            rollback_record_digest=rollback_digest,
            request_path=evidence_filename("request", request.digest),
            outcome_path=evidence_filename("outcome", outcome.digest),
            rollback_record_path=(
                evidence_filename("rollback", rollback_digest)
                if rollback_digest is not None
                else None
            ),
        ),
    )


def _load_model(path: Path, model_type, label: str):
    if not path.is_file():
        raise RegressionRecoveryEvidenceVerificationError(
            f"missing {label} evidence file: {path.name}"
        )
    try:
        return model_type.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise RegressionRecoveryEvidenceVerificationError(
            f"invalid {label} evidence file {path.name}: {error}"
        ) from error


def _verify_known_prefix_files(evidence_dir: Path, expected: set[str]) -> None:
    for path in evidence_dir.iterdir():
        if not path.is_file() or _KNOWN_PREFIX.match(path.name) is None:
            continue
        if re.fullmatch(r"(request|outcome|rollback)-[0-9a-f]{64}\.json", path.name) is None:
            raise RegressionRecoveryEvidenceVerificationError(
                f"malformed known-prefix evidence file: {path.name}"
            )
        if path.name not in expected:
            raise RegressionRecoveryEvidenceVerificationError(
                f"orphan known-prefix evidence file: {path.name}"
            )


def verify_regression_recovery_evidence(
    evidence_dir: Path,
) -> RegressionRecoveryEvidenceVerification:
    """Verify internal consistency only, never evidence authenticity.

    Authenticity requires an external signature, manifest, or trusted anchor.
    This function sets no threshold, performs no recovery decision, and never
    writes configuration.
    """

    index_path = evidence_dir / REGRESSION_RECOVERY_EVIDENCE_INDEX_FILENAME
    if not index_path.is_file():
        raise RegressionRecoveryEvidenceVerificationError(
            f"missing fixed index: {REGRESSION_RECOVERY_EVIDENCE_INDEX_FILENAME}"
        )
    try:
        index = RegressionRecoveryEvidenceIndex.model_validate_json(
            index_path.read_text(encoding="utf-8")
        )
    except Exception as error:
        raise RegressionRecoveryEvidenceVerificationError(
            f"invalid regression evidence index: {error}"
        ) from error
    request = _load_model(
        evidence_dir / index.request_path, RegressionRecoveryRequest, "request"
    )
    outcome = _load_model(
        evidence_dir / index.outcome_path, RegressionRecoveryOutcome, "outcome"
    )
    rollback = None
    if index.rollback_record_path is not None:
        rollback = _load_model(
            evidence_dir / index.rollback_record_path, RollbackRecord, "rollback"
        )
    if request.digest != index.request_digest:
        raise RegressionRecoveryEvidenceVerificationError("request digest mismatch")
    if outcome.digest != index.outcome_digest:
        raise RegressionRecoveryEvidenceVerificationError("outcome digest mismatch")
    if rollback is not None and rollback.digest != index.rollback_record_digest:
        raise RegressionRecoveryEvidenceVerificationError("rollback digest mismatch")
    if rollback != outcome.rollback_record:
        raise RegressionRecoveryEvidenceVerificationError(
            "rollback file does not match outcome rollback"
        )
    try:
        RegressionRecoveryEvidenceGraph(
            request=request, outcome=outcome, index=index
        )
    except Exception as error:
        raise RegressionRecoveryEvidenceVerificationError(
            f"regression evidence association failure: {error}"
        ) from error
    expected = {index.request_path, index.outcome_path}
    if index.rollback_record_path is not None:
        expected.add(index.rollback_record_path)
    _verify_known_prefix_files(evidence_dir, expected)
    return RegressionRecoveryEvidenceVerification(
        index_digest=index.digest,
        request_digest=request.digest,
        outcome_digest=outcome.digest,
        rollback_record_digest=(rollback.digest if rollback is not None else None),
        status=outcome.status,
        stop_required=outcome.stop_required,
        rollback_present=rollback is not None,
    )


__all__ = [
    "REGRESSION_RECOVERY_EVIDENCE_INDEX_FILENAME",
    "REGRESSION_RECOVERY_EVIDENCE_INDEX_SCHEMA",
    "REGRESSION_RECOVERY_EVIDENCE_VERIFICATION_SCHEMA",
    "RegressionRecoveryEvidenceGraph",
    "RegressionRecoveryEvidenceIndex",
    "RegressionRecoveryEvidenceVerification",
    "RegressionRecoveryEvidenceVerificationError",
    "build_regression_recovery_evidence_graph",
    "evidence_filename",
    "verify_regression_recovery_evidence",
]
