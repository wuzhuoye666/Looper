"""Durable, content-addressed journals for D5 two-phase interventions.

The store proves internal chain integrity and execution association. It is not a
truth anchor and never replays backend writes; a non-terminal head is evidence
for the existing lease/state reconciliation path.

Deployment hard gate (RCP-02A): before upgrading to this advisory-lock store,
every legacy ``O_CREAT | O_EXCL`` guard writer must be stopped.  The new advisory
lock protocol and the legacy guard protocol share no mutual-exclusion primitive,
so they must never run concurrently against the same receipt root.  A legacy
``.<64hex>.<candidate|recovery>.guard`` file is never deleted here; it is only
detected and fails the whole store closed, awaiting explicit RCP-02B
reconciliation.
"""

from __future__ import annotations

import errno
import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.intervention import (
    InterventionExecutionReceiptV2,
    InterventionPlan,
    ReceiptOperation,
    ReceiptStageV2,
)

RECEIPT_POINTER_SCHEMA = "looper.intervention-receipt-pointer/v1alpha1"
_DIGEST = r"^sha256:[0-9a-f]{64}$"
_CONTENT_NAME = re.compile(r"^[0-9a-f]{64}\.json$")
_POINTER_NAME = re.compile(r"^[0-9a-f]{64}\.(candidate|recovery)\.current\.json$")
_LEGACY_GUARD_NAME = re.compile(r"^\.[0-9a-f]{64}\.(candidate|recovery)\.guard$")
_SENTINEL = b"\x00"
_NETWORK_FS_TYPES = frozenset(
    {
        "9p",
        "afs",
        "ceph",
        "cifs",
        "davfs",
        "fuse.ceph",
        "fuse.sshfs",
        "glusterfs",
        "lustre",
        "ncpfs",
        "nfs",
        "nfs4",
        "smb3",
        "smbfs",
        "sshfs",
    }
)
_ALLOWED_SUCCESSORS = {
    ReceiptStageV2.PLANNED: {
        ReceiptStageV2.PREFLIGHT_COMPLETED,
        ReceiptStageV2.SAFETY_TERMINAL,
    },
    ReceiptStageV2.PREFLIGHT_COMPLETED: {
        ReceiptStageV2.APPLY_STARTED,
        ReceiptStageV2.SAFETY_TERMINAL,
    },
    ReceiptStageV2.APPLY_STARTED: {
        ReceiptStageV2.ROLLBACK_ATTEMPTED,
        ReceiptStageV2.SAFETY_TERMINAL,
    },
    ReceiptStageV2.ROLLBACK_ATTEMPTED: {
        ReceiptStageV2.ROLLBACK_VERIFIED,
        ReceiptStageV2.SAFETY_TERMINAL,
    },
    ReceiptStageV2.ROLLBACK_VERIFIED: {ReceiptStageV2.SAFETY_TERMINAL},
    ReceiptStageV2.SAFETY_TERMINAL: {ReceiptStageV2.OPERATION_TERMINAL},
    ReceiptStageV2.OPERATION_TERMINAL: set(),
}

AttentionSink = Callable[[str, str], None]


def _native_path(path: Path) -> str:
    """Return an absolute Windows extended path so receipt names survive MAX_PATH."""

    value = str(path.absolute())
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value.lstrip("\\")
    return "\\\\?\\" + value


def _try_lock(fd: int) -> None:
    """Acquire a non-blocking exclusive process advisory lock on byte 0 of ``fd``.

    Windows ``msvcrt.locking`` locks ``nbytes`` bytes from the *current file
    position*, so the descriptor is always seeked to 0 first to make every
    descriptor race on the same sentinel byte.  ``flock`` has no file-position
    semantics.
    """
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[union-attr]
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[union-attr]


def _unlock(fd: int) -> None:
    """Release the byte-0 process advisory lock previously taken by ``_try_lock``."""
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[union-attr]
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[union-attr]


def _is_lock_busy(error: OSError) -> bool:
    """Distinguish a real lock contention from a lock-capability failure."""
    if os.name == "nt":
        return error.errno in (errno.EACCES, errno.EDEADLK)
    return error.errno in (errno.EWOULDBLOCK, errno.EAGAIN)


class ReceiptStoreError(RuntimeError):
    """Raised when a durable receipt chain cannot be trusted."""


class ReceiptStoreUnavailable(RuntimeError):
    """The store's filesystem cannot provably support process advisory locking.

    Distinct from ``ReceiptStoreError("receipt chain is busy")``: this signals a
    lock-capability or filesystem failure, never a legitimate lock contention.
    """


class InterventionReceiptPointer(StrictModel):
    schema_version: Literal[RECEIPT_POINTER_SCHEMA] = RECEIPT_POINTER_SCHEMA
    execution_digest: str = Field(pattern=_DIGEST)
    plan_digest: str = Field(pattern=_DIGEST)
    execution_id: str = Field(min_length=1, max_length=160)
    operation: ReceiptOperation
    receipt_digest: str = Field(pattern=_DIGEST)
    receipt_filename: str = Field(pattern=r"^[0-9a-f]{64}\.json$")

    @model_validator(mode="after")
    def validate_filename(self) -> InterventionReceiptPointer:
        if self.receipt_filename != f"{self.receipt_digest.removeprefix('sha256:')}.json":
            raise ValueError("receipt pointer filename does not match its digest")
        expected = canonical_digest(
            {"execution_id": self.execution_id, "plan_digest": self.plan_digest}
        )
        if self.execution_digest != expected:
            raise ValueError("receipt pointer execution digest is invalid")
        return self


class DurableReceiptStore:
    """Persist and verify immutable receipt nodes plus per-operation head pointers."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock_support_verified = False

    @staticmethod
    def _execution_digest(plan_digest: str, execution_id: str) -> str:
        return canonical_digest({"execution_id": execution_id, "plan_digest": plan_digest})

    def _pointer_path(
        self, plan_digest: str, execution_id: str, operation: ReceiptOperation
    ) -> Path:
        identity = self._execution_digest(plan_digest, execution_id).removeprefix("sha256:")
        return self.root / f"{identity}.{operation.value}.current.json"

    def _content_path(self, receipt_digest: str) -> Path:
        return self.root / f"{receipt_digest.removeprefix('sha256:')}.json"

    def _lock_path(
        self, plan_digest: str, execution_id: str, operation: ReceiptOperation
    ) -> Path:
        identity = self._execution_digest(plan_digest, execution_id).removeprefix("sha256:")
        return self.root / f".{identity}.{operation.value}.lock"

    def _assert_no_legacy_guard(self) -> None:
        """Fail the whole store closed when any legacy ``.guard`` file remains."""
        native_root = _native_path(self.root)
        if not os.path.isdir(native_root):
            return
        for entry in os.scandir(native_root):
            if entry.is_file(follow_symlinks=False) and _LEGACY_GUARD_NAME.fullmatch(
                entry.name
            ):
                raise ReceiptStoreError(
                    "legacy receipt guard requires explicit reconciliation"
                )

    def _assert_local_filesystem(self) -> None:
        """Reject provably non-local filesystems before taking any lock.

        Windows: reject explicit UNC/remote roots.  Linux: reject mount types
        known to be network filesystems.  Anything the standard library cannot
        reliably classify is left to the exclusion self-test, which is the final
        authority on provable mutual exclusion.
        """
        if os.name == "nt":
            drive = os.path.splitdrive(str(self.root.absolute()))[0]
            if drive.startswith("\\\\"):
                raise ReceiptStoreUnavailable(
                    "receipt store on a UNC/remote volume is not supported"
                )
            return
        fstype = self._linux_mount_fstype()
        if fstype in _NETWORK_FS_TYPES:
            raise ReceiptStoreUnavailable(
                f"receipt store on a network filesystem is not supported: {fstype}"
            )

    def _linux_mount_fstype(self) -> str | None:
        try:
            with open("/proc/mounts", encoding="utf-8") as stream:
                lines = stream.read().splitlines()
        except OSError:
            return None
        root = str(self.root.resolve())
        best_type: str | None = None
        best_point = ""
        for line in lines:
            parts = line.split()
            if len(parts) < 3:
                continue
            point = parts[1].replace("\\040", " ")
            if (
                point == "/" or root == point or root.startswith(point + "/")
            ) and len(point) > len(best_point):
                best_point = point
                best_type = parts[2]
        return best_type

    @staticmethod
    def _ensure_sentinel(fd: int) -> None:
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, _SENTINEL)
        except OSError as error:
            raise ReceiptStoreUnavailable(
                f"cannot write lock sentinel byte: {error}"
            ) from error

    def _acquire_lock(self, path: Path) -> int:
        try:
            fd = os.open(_native_path(path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as error:
            raise ReceiptStoreUnavailable(f"cannot open receipt lock: {error}") from error
        try:
            self._ensure_sentinel(fd)
            try:
                _try_lock(fd)
            except OSError as error:
                if _is_lock_busy(error):
                    raise ReceiptStoreError("receipt chain is busy") from error
                raise ReceiptStoreUnavailable(
                    f"cannot acquire receipt lock: {error}"
                ) from error
            return fd
        except Exception:
            os.close(fd)
            raise

    def _verify_lock_exclusion(self, path: Path) -> None:
        """Prove two independent opens of the lock file cannot both hold the lock."""
        try:
            probe_fd = os.open(_native_path(path), os.O_RDWR)
        except OSError as error:
            raise ReceiptStoreUnavailable(
                f"cannot probe lock exclusion: {error}"
            ) from error
        try:
            try:
                _try_lock(probe_fd)
            except OSError as error:
                if _is_lock_busy(error):
                    return
                raise ReceiptStoreUnavailable(
                    f"lock probe failed unexpectedly: {error}"
                ) from error
            raise ReceiptStoreUnavailable(
                "advisory lock is not mutually exclusive on this filesystem"
            )
        finally:
            with suppress(OSError):
                _unlock(probe_fd)
            with suppress(OSError):
                os.close(probe_fd)

    @contextmanager
    def _mutex(
        self, plan_digest: str, execution_id: str, operation: ReceiptOperation
    ) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        self._assert_no_legacy_guard()
        if not self._lock_support_verified:
            self._assert_local_filesystem()
        lock_path = self._lock_path(plan_digest, execution_id, operation)
        fd = self._acquire_lock(lock_path)
        try:
            if not self._lock_support_verified:
                self._verify_lock_exclusion(lock_path)
                self._lock_support_verified = True
            yield
        finally:
            try:
                _unlock(fd)
            finally:
                os.close(fd)

    @staticmethod
    def _atomic_write(path: Path, payload: dict[str, object]) -> None:
        temporary = path.parent / f".receipt-{os.getpid()}-{uuid4().hex}.tmp"
        try:
            with open(_native_path(temporary), "w", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
            os.replace(_native_path(temporary), _native_path(path))
        finally:
            if os.path.exists(_native_path(temporary)):
                os.unlink(_native_path(temporary))

    @staticmethod
    def _read_receipt(path: Path) -> InterventionExecutionReceiptV2:
        try:
            with open(_native_path(path), encoding="utf-8") as stream:
                receipt = InterventionExecutionReceiptV2.model_validate_json(stream.read())
        except Exception as error:
            raise ReceiptStoreError(f"invalid receipt file: {path.name}") from error
        if path.name != f"{receipt.digest.removeprefix('sha256:')}.json":
            raise ReceiptStoreError(f"receipt filename digest mismatch: {path.name}")
        return receipt

    def _read_pointer(self, path: Path) -> InterventionReceiptPointer | None:
        if not os.path.exists(_native_path(path)):
            return None
        try:
            with open(_native_path(path), encoding="utf-8") as stream:
                return InterventionReceiptPointer.model_validate_json(stream.read())
        except Exception as error:
            raise ReceiptStoreError(f"invalid receipt pointer: {path.name}") from error

    def _all_receipts(self) -> dict[str, InterventionExecutionReceiptV2]:
        if not os.path.exists(_native_path(self.root)):
            return {}
        receipts: dict[str, InterventionExecutionReceiptV2] = {}
        for path in sorted(self.root.glob("*.json"), key=lambda candidate: candidate.name):
            if path.name.endswith(".current.json"):
                if not _POINTER_NAME.fullmatch(path.name):
                    raise ReceiptStoreError(f"malformed receipt pointer filename: {path.name}")
                pointer = self._read_pointer(path)
                assert pointer is not None
                expected = (
                    f"{pointer.execution_digest.removeprefix('sha256:')}."
                    f"{pointer.operation.value}.current.json"
                )
                if path.name != expected:
                    raise ReceiptStoreError(f"receipt pointer filename mismatch: {path.name}")
                continue
            if not _CONTENT_NAME.fullmatch(path.name):
                raise ReceiptStoreError(f"malformed receipt filename: {path.name}")
            receipt = self._read_receipt(path)
            if receipt.digest in receipts:
                raise ReceiptStoreError(f"duplicate receipt digest: {receipt.digest}")
            receipts[receipt.digest] = receipt
        successors: set[str] = set()
        for receipt in receipts.values():
            if receipt.sequence == 0:
                continue
            predecessor_digest = receipt.predecessor_receipt_digest
            assert predecessor_digest is not None
            predecessor = receipts.get(predecessor_digest)
            if predecessor is None:
                raise ReceiptStoreError("receipt chain has a missing predecessor")
            if predecessor_digest in successors:
                raise ReceiptStoreError("receipt chain forked from one predecessor")
            self._validate_successor(predecessor, receipt)
            successors.add(predecessor_digest)
        return receipts

    @staticmethod
    def _validate_successor(
        predecessor: InterventionExecutionReceiptV2,
        successor: InterventionExecutionReceiptV2,
    ) -> None:
        if successor.sequence != predecessor.sequence + 1:
            raise ReceiptStoreError("receipt sequence is not contiguous")
        if successor.predecessor_receipt_digest != predecessor.digest:
            raise ReceiptStoreError("receipt predecessor digest is invalid")
        if successor.stage not in _ALLOWED_SUCCESSORS[predecessor.stage]:
            raise ReceiptStoreError(
                f"illegal receipt stage transition: {predecessor.stage.value} -> "
                f"{successor.stage.value}"
            )
        if (
            successor.plan_digest != predecessor.plan_digest
            or successor.execution_id != predecessor.execution_id
            or successor.operation is not predecessor.operation
            or successor.parent_receipt_digest != predecessor.parent_receipt_digest
        ):
            raise ReceiptStoreError("receipt chain identity drifted")

    def _verify_recovery_parent(
        self,
        receipt: InterventionExecutionReceiptV2,
        all_receipts: dict[str, InterventionExecutionReceiptV2],
    ) -> None:
        if receipt.operation is not ReceiptOperation.RECOVERY:
            return
        parent = all_receipts.get(receipt.parent_receipt_digest or "")
        if parent is None:
            raise ReceiptStoreError("recovery parent receipt is missing")
        if (
            parent.operation is not ReceiptOperation.CANDIDATE
            or parent.stage is not ReceiptStageV2.SAFETY_TERMINAL
            or parent.plan_digest != receipt.plan_digest
            or parent.execution_id != receipt.execution_id
        ):
            raise ReceiptStoreError("recovery parent is not the matching candidate safety terminal")
        self.verify_chain(
            receipt.plan_digest,
            receipt.execution_id,
            ReceiptOperation.CANDIDATE,
        )

    def _scope_receipts(
        self,
        plan_digest: str,
        execution_id: str,
        operation: ReceiptOperation,
    ) -> tuple[
        dict[str, InterventionExecutionReceiptV2],
        dict[str, InterventionExecutionReceiptV2],
    ]:
        all_receipts = self._all_receipts()
        scoped = {
            digest: receipt
            for digest, receipt in all_receipts.items()
            if receipt.plan_digest == plan_digest
            and receipt.execution_id == execution_id
            and receipt.operation is operation
        }
        return all_receipts, scoped

    def head(
        self,
        plan_digest: str,
        execution_id: str,
        operation: ReceiptOperation,
    ) -> InterventionExecutionReceiptV2 | None:
        all_receipts, scoped = self._scope_receipts(plan_digest, execution_id, operation)
        pointer_path = self._pointer_path(plan_digest, execution_id, operation)
        pointer = self._read_pointer(pointer_path)
        if not scoped:
            if pointer is not None:
                raise ReceiptStoreError("receipt pointer exists without a chain")
            return None

        roots = [receipt for receipt in scoped.values() if receipt.sequence == 0]
        if len(roots) != 1:
            raise ReceiptStoreError("receipt chain must have exactly one root")
        root = roots[0]
        if root.predecessor_receipt_digest is not None:
            raise ReceiptStoreError("receipt root has a predecessor")
        self._verify_recovery_parent(root, all_receipts)

        successors: dict[str, InterventionExecutionReceiptV2] = {}
        for receipt in scoped.values():
            if receipt.sequence == 0:
                continue
            predecessor_digest = receipt.predecessor_receipt_digest
            assert predecessor_digest is not None
            predecessor = scoped.get(predecessor_digest)
            if predecessor is None:
                raise ReceiptStoreError("receipt chain has a missing predecessor")
            if predecessor_digest in successors:
                raise ReceiptStoreError("receipt chain forked from one predecessor")
            self._validate_successor(predecessor, receipt)
            successors[predecessor_digest] = receipt

        visited: set[str] = set()
        current = root
        while True:
            if current.digest in visited:
                raise ReceiptStoreError("receipt chain contains a cycle")
            visited.add(current.digest)
            successor = successors.get(current.digest)
            if successor is None:
                break
            current = successor
        if visited != set(scoped):
            raise ReceiptStoreError("receipt chain contains disconnected nodes")

        if pointer is not None:
            if (
                pointer.plan_digest != plan_digest
                or pointer.execution_id != execution_id
                or pointer.operation is not operation
            ):
                raise ReceiptStoreError("receipt pointer identity mismatch")
            pointed = scoped.get(pointer.receipt_digest)
            if (
                pointed is None
                or pointer.receipt_filename != self._content_path(pointed.digest).name
            ):
                raise ReceiptStoreError("receipt pointer is dangling")
            # A pointer to a valid ancestor is a recoverable content-before-pointer
            # crash seam. The unique chain head remains authoritative.
        return current

    def verify_chain(
        self,
        plan_digest: str,
        execution_id: str,
        operation: ReceiptOperation,
    ) -> InterventionExecutionReceiptV2:
        head = self.head(plan_digest, execution_id, operation)
        if head is None:
            raise ReceiptStoreError("receipt chain does not exist")
        return head

    def heads(self) -> list[InterventionExecutionReceiptV2]:
        """Return every verified operation head in deterministic identity order."""

        receipts = self._all_receipts()
        scopes = {
            (receipt.plan_digest, receipt.execution_id, receipt.operation)
            for receipt in receipts.values()
        }
        if os.path.exists(_native_path(self.root)):
            for path in self.root.glob("*.current.json"):
                pointer = self._read_pointer(path)
                assert pointer is not None
                scopes.add(
                    (pointer.plan_digest, pointer.execution_id, pointer.operation)
                )
        heads: list[InterventionExecutionReceiptV2] = []
        for plan_digest, execution_id, operation in sorted(
            scopes, key=lambda scope: (scope[0], scope[1], scope[2].value)
        ):
            head = self.head(plan_digest, execution_id, operation)
            if head is None:
                raise ReceiptStoreError("receipt pointer exists without a chain")
            heads.append(head)
        return heads

    def _publish_receipt(self, receipt: InterventionExecutionReceiptV2) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._content_path(receipt.digest)
        if os.path.exists(_native_path(path)):
            existing = self._read_receipt(path)
            if existing != receipt:
                raise ReceiptStoreError("existing receipt content is not idempotent")
        else:
            self._atomic_write(path, receipt.model_dump(mode="json", exclude_none=False))
        pointer = InterventionReceiptPointer(
            execution_digest=receipt.execution_digest,
            plan_digest=receipt.plan_digest,
            execution_id=receipt.execution_id,
            operation=receipt.operation,
            receipt_digest=receipt.digest,
            receipt_filename=path.name,
        )
        self._atomic_write(
            self._pointer_path(receipt.plan_digest, receipt.execution_id, receipt.operation),
            pointer.model_dump(mode="json", exclude_none=False),
        )

    def start(
        self,
        *,
        plan: InterventionPlan,
        execution_id: str,
        operation: ReceiptOperation,
        parent_receipt_digest: str | None = None,
    ) -> InterventionExecutionReceiptV2:
        with self._mutex(plan.digest, execution_id, operation):
            if self.head(plan.digest, execution_id, operation) is not None:
                raise ReceiptStoreError("receipt execution already exists")
            receipt = InterventionExecutionReceiptV2(
                plan_digest=plan.digest,
                execution_id=execution_id,
                operation=operation,
                stage=ReceiptStageV2.PLANNED,
                sequence=0,
                parent_receipt_digest=parent_receipt_digest,
                plan=plan if operation is ReceiptOperation.CANDIDATE else None,
            )
            if operation is ReceiptOperation.RECOVERY:
                all_receipts = self._all_receipts()
                self._verify_recovery_parent(receipt, all_receipts)
            self._publish_receipt(receipt)
            return receipt

    def advance(
        self,
        current: InterventionExecutionReceiptV2,
        stage: ReceiptStageV2,
        **fields: object,
    ) -> InterventionExecutionReceiptV2:
        protected = {
            "schema_version",
            "plan_digest",
            "execution_id",
            "operation",
            "stage",
            "sequence",
            "predecessor_receipt_digest",
            "parent_receipt_digest",
            "plan",
        }
        overlap = protected & set(fields)
        if overlap:
            raise ReceiptStoreError(f"advance cannot override identity fields: {sorted(overlap)}")
        with self._mutex(current.plan_digest, current.execution_id, current.operation):
            candidate = InterventionExecutionReceiptV2(
                plan_digest=current.plan_digest,
                execution_id=current.execution_id,
                operation=current.operation,
                stage=stage,
                sequence=current.sequence + 1,
                predecessor_receipt_digest=current.digest,
                parent_receipt_digest=current.parent_receipt_digest,
                **fields,
            )
            self._validate_successor(current, candidate)
            head = self.verify_chain(current.plan_digest, current.execution_id, current.operation)
            if head.digest != current.digest:
                if head == candidate:
                    self._publish_receipt(head)
                    return head
                raise ReceiptStoreError("cannot advance a stale receipt head")
            self._publish_receipt(candidate)
            return candidate


__all__ = [
    "AttentionSink",
    "DurableReceiptStore",
    "InterventionReceiptPointer",
    "RECEIPT_POINTER_SCHEMA",
    "ReceiptStoreError",
]
