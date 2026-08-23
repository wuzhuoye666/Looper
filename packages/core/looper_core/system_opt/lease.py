from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import Field

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel


class LeaseConflict(RuntimeError):
    pass


class TargetLease(StrictModel):
    target_id: str = Field(min_length=1, max_length=200)
    owner_id: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1)
    acquired_at: datetime
    expires_at: datetime
    reconciliation_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class TargetAttention(StrictModel):
    target_id: str
    reason: str
    evidence_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    recorded_at: datetime


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

    def acquire(
        self,
        target_id: str,
        owner_id: str,
        *,
        ttl_seconds: float,
        now: datetime,
        reconciliation_digest: str | None,
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
                if reconciliation_digest is None:
                    raise LeaseConflict("expired lease takeover requires reconciliation evidence")
                token = existing.fencing_token + 1
            else:
                token = 1
            lease = TargetLease(
                target_id=target_id,
                owner_id=owner_id,
                fencing_token=token,
                acquired_at=now.astimezone(UTC),
                expires_at=(now + timedelta(seconds=ttl_seconds)).astimezone(UTC),
                reconciliation_digest=reconciliation_digest,
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

    def clear_attention(self, target_id: str, *, reconciliation_digest: str) -> None:
        if not reconciliation_digest.startswith("sha256:"):
            raise ValueError("reconciliation digest is invalid")
        path = self._attention_path(target_id)
        if path.exists():
            path.unlink()


__all__ = [
    "FileTargetGuard",
    "LeaseConflict",
    "TargetAttention",
    "TargetLease",
]
