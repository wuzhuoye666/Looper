from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.executor import ConfigSnapshot


class LeaseConflict(RuntimeError):
    pass


class TargetLease(StrictModel):
    target_id: str = Field(min_length=1, max_length=200)
    owner_id: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1)
    acquired_at: datetime
    expires_at: datetime
    reconciliation_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class TargetAttention(StrictModel):
    target_id: str
    reason: str
    evidence_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    recorded_at: datetime


class ReconciliationOutcome(StrEnum):
    MATCHED_SNAPSHOT = "matched-snapshot"
    NEEDS_ATTENTION = "needs-attention"


class TargetReconciliation(StrictModel):
    target_id: str = Field(min_length=1, max_length=200)
    previous_lease_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    actual_snapshot: ConfigSnapshot
    expected_snapshot: ConfigSnapshot
    outcome: ReconciliationOutcome
    reason: str = Field(min_length=1, max_length=2000)
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_outcome(self) -> TargetReconciliation:
        if self.recorded_at.tzinfo is None:
            raise ValueError("reconciliation timestamp must be timezone-aware")
        if self.actual_snapshot.target_id != self.target_id:
            raise ValueError("actual snapshot target does not match reconciliation target")
        if self.expected_snapshot.target_id != self.target_id:
            raise ValueError("expected snapshot target does not match reconciliation target")
        if self.outcome == ReconciliationOutcome.MATCHED_SNAPSHOT:
            if not self.actual_snapshot.complete or not self.expected_snapshot.complete:
                raise ValueError("matched reconciliation requires complete snapshots")
            if self.actual_snapshot.digest != self.expected_snapshot.digest:
                raise ValueError("matched reconciliation requires equal snapshots")
        return self

    @property
    def actual_snapshot_digest(self) -> str:
        return self.actual_snapshot.digest

    @property
    def expected_snapshot_digest(self) -> str:
        return self.expected_snapshot.digest

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class TargetRecoveryEvidence(StrictModel):
    target_id: str = Field(min_length=1, max_length=200)
    attention_evidence_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    actual_snapshot: ConfigSnapshot
    approved_snapshot: ConfigSnapshot
    reason: str = Field(min_length=1, max_length=2000)
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_recovery(self) -> TargetRecoveryEvidence:
        if self.recorded_at.tzinfo is None:
            raise ValueError("recovery timestamp must be timezone-aware")
        if self.actual_snapshot.target_id != self.target_id:
            raise ValueError("actual snapshot target does not match recovery target")
        if self.approved_snapshot.target_id != self.target_id:
            raise ValueError("approved snapshot target does not match recovery target")
        if not self.actual_snapshot.complete or not self.approved_snapshot.complete:
            raise ValueError("recovery requires complete snapshots")
        if self.actual_snapshot.digest != self.approved_snapshot.digest:
            raise ValueError("recovery requires actual and approved snapshots to match")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class FileTargetGuard:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _name(target_id: str) -> str:
        return canonical_digest({"target_id": target_id}).removeprefix("sha256:")

    def _lease_path(self, target_id: str) -> Path:
        return self._root / f"{self._name(target_id)}.lease.json"

    def _attention_path(self, target_id: str) -> Path:
        return self._root / f"{self._name(target_id)}.attention.json"

    def _mutex_path(self, target_id: str) -> Path:
        return self._root / f"{self._name(target_id)}.guard"

    @contextmanager
    def _mutex(self, target_id: str) -> Iterator[None]:
        path = self._mutex_path(target_id)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise LeaseConflict(f"target {target_id!r} lease store is busy") from error
        try:
            os.close(descriptor)
            yield
        finally:
            if path.exists():
                path.unlink()

    @staticmethod
    def _read(path: Path, model: type[StrictModel]) -> StrictModel | None:
        if not path.exists():
            return None
        return model.model_validate(json.loads(path.read_text(encoding="utf-8")))

    @staticmethod
    def _atomic_write(path: Path, payload: dict[str, object]) -> None:
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def assert_writable(self, target_id: str) -> None:
        attention = self._read(self._attention_path(target_id), TargetAttention)
        if attention is not None:
            assert isinstance(attention, TargetAttention)
            raise LeaseConflict(f"target {target_id!r} needs attention: {attention.reason}")

    def current_lease(self, target_id: str) -> TargetLease | None:
        lease = self._read(self._lease_path(target_id), TargetLease)
        assert lease is None or isinstance(lease, TargetLease)
        return lease

    def current_attention(self, target_id: str) -> TargetAttention | None:
        attention = self._read(self._attention_path(target_id), TargetAttention)
        assert attention is None or isinstance(attention, TargetAttention)
        return attention

    def acquire(
        self,
        target_id: str,
        owner_id: str,
        *,
        ttl_seconds: float,
        now: datetime,
        reconciliation: TargetReconciliation | None,
    ) -> TargetLease:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl_seconds must be positive")
        if now.tzinfo is None:
            raise ValueError("lease timestamps must be timezone-aware")
        with self._mutex(target_id):
            self.assert_writable(target_id)
            path = self._lease_path(target_id)
            existing = self._read(path, TargetLease)
            if existing is not None:
                assert isinstance(existing, TargetLease)
                if existing.expires_at > now:
                    raise LeaseConflict(f"target {target_id!r} is leased by {existing.owner_id!r}")
                if reconciliation is None:
                    raise LeaseConflict("expired lease takeover requires reconciliation evidence")
                if reconciliation.target_id != target_id:
                    raise LeaseConflict("reconciliation target does not match the lease target")
                if reconciliation.previous_lease_digest != existing.digest:
                    raise LeaseConflict("reconciliation does not bind the expired lease")
                if reconciliation.outcome != ReconciliationOutcome.MATCHED_SNAPSHOT:
                    raise LeaseConflict("reconciliation outcome requires target attention")
                token = existing.fencing_token + 1
            else:
                if reconciliation is not None:
                    raise LeaseConflict("reconciliation was supplied without an expired lease")
                token = 1
            lease = TargetLease(
                target_id=target_id,
                owner_id=owner_id,
                fencing_token=token,
                acquired_at=now.astimezone(UTC),
                expires_at=(now + timedelta(seconds=ttl_seconds)).astimezone(UTC),
                reconciliation_digest=(
                    reconciliation.digest if reconciliation is not None else None
                ),
            )
            self._atomic_write(path, lease.model_dump(mode="json", exclude_none=True))
            return lease

    def release(self, lease: TargetLease) -> None:
        with self._mutex(lease.target_id):
            path = self._lease_path(lease.target_id)
            current = self._read(path, TargetLease)
            if current is None:
                return
            assert isinstance(current, TargetLease)
            if current.owner_id != lease.owner_id or current.fencing_token != lease.fencing_token:
                raise LeaseConflict("lease release identity does not match the current writer")
            path.unlink()

    def mark_needs_attention(
        self, target_id: str, *, reason: str, evidence_digest: str, now: datetime
    ) -> TargetAttention:
        attention = TargetAttention(
            target_id=target_id,
            reason=reason,
            evidence_digest=evidence_digest,
            recorded_at=now.astimezone(UTC),
        )
        self._atomic_write(
            self._attention_path(target_id),
            attention.model_dump(mode="json"),
        )
        return attention

    def clear_attention(self, target_id: str, *, recovery: TargetRecoveryEvidence) -> None:
        path = self._attention_path(target_id)
        attention = self._read(path, TargetAttention)
        if attention is None:
            raise LeaseConflict(f"target {target_id!r} has no attention record")
        assert isinstance(attention, TargetAttention)
        if recovery.target_id != target_id:
            raise LeaseConflict("recovery target does not match the attention target")
        if recovery.attention_evidence_digest != attention.evidence_digest:
            raise LeaseConflict("recovery does not bind the attention evidence")
        path.unlink()


__all__ = [
    "FileTargetGuard",
    "LeaseConflict",
    "ReconciliationOutcome",
    "TargetAttention",
    "TargetLease",
    "TargetReconciliation",
    "TargetRecoveryEvidence",
]
