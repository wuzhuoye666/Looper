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

import ctypes
import errno
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from looper_core.canonical import utc_now

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
_SUPPORTED_LOCAL_LINUX_FS_TYPES = frozenset({"btrfs", "ext2", "ext3", "ext4", "xfs"})
_WINDOWS_DRIVE_FIXED = 3
_WINDOWS_DRIVE_REMOTE = 4
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


class ReceiptStoreUnavailable(ReceiptStoreError):
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


class GuardReconciliationOutcome(StrEnum):
    ORPHAN_CONFIRMED = "orphan-confirmed"
    NEEDS_ATTENTION = "needs-attention"


class GuardWriterQuiescence(StrictModel):
    """Operator declaration that every legacy guard writer has been stopped."""

    declared: bool
    statement: str = Field(min_length=1, max_length=2000)


class ReceiptGuardReconciliation(StrictModel):
    """RCP-02B evidence that a legacy ``.guard`` was explicitly reconciled.

    A legacy guard is an empty file; the only recoverable facts are its
    filename-encoded identity/operation and its byte digest.  The evidence is
    content-addressed and must be durably published before the guard file is
    deleted (frozen order, contract receipt-mutex-recovery-contract §8).
    """

    schema_version: Literal["looper.receipt-guard-reconciliation/v1alpha1"] = (
        "looper.receipt-guard-reconciliation/v1alpha1"
    )
    guard_filename: str = Field(pattern=r"^\.[0-9a-f]{64}\.(candidate|recovery)\.guard$")
    execution_digest: str = Field(pattern=_DIGEST)
    operation: ReceiptOperation
    guard_sha256: str = Field(pattern=_DIGEST)
    receipt_root: str = Field(min_length=1, max_length=4096)
    discovered_at: datetime
    target_id: str = Field(min_length=1, max_length=200)
    operator_id: str = Field(min_length=1, max_length=200)
    writer_quiescence: GuardWriterQuiescence
    chain_head_digest: str | None = Field(default=None, pattern=_DIGEST)
    outcome: GuardReconciliationOutcome
    reason: str = Field(min_length=1, max_length=2000)
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_guard_identity(self) -> ReceiptGuardReconciliation:
        if self.discovered_at.tzinfo is None or self.recorded_at.tzinfo is None:
            raise ValueError("guard reconciliation timestamps must be timezone-aware")
        expected_name = (
            f".{self.execution_digest.removeprefix('sha256:')}."
            f"{self.operation.value}.guard"
        )
        if self.guard_filename != expected_name:
            raise ValueError("guard filename does not encode the declared identity")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


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
            if _LEGACY_GUARD_NAME.fullmatch(entry.name):
                raise ReceiptStoreError(
                    "legacy receipt guard requires explicit reconciliation"
                )

    @staticmethod
    def _assert_supported_linux_filesystem(fstype: str | None) -> None:
        if fstype is None:
            raise ReceiptStoreUnavailable(
                "receipt store filesystem type cannot be determined"
            )
        if fstype in _NETWORK_FS_TYPES:
            raise ReceiptStoreUnavailable(
                f"receipt store on a network filesystem is not supported: {fstype}"
            )
        if fstype not in _SUPPORTED_LOCAL_LINUX_FS_TYPES:
            raise ReceiptStoreUnavailable(
                f"receipt store filesystem type is not in the local allowlist: {fstype}"
            )

    @staticmethod
    def _windows_drive_type(root: Path) -> int:
        drive = os.path.splitdrive(str(root.absolute()))[0]
        if drive.startswith("\\\\"):
            return _WINDOWS_DRIVE_REMOTE
        if not drive:
            return 0
        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW  # type: ignore[attr-defined]
        get_drive_type.argtypes = [ctypes.c_wchar_p]
        get_drive_type.restype = ctypes.c_uint
        return int(get_drive_type(f"{drive}\\"))

    def _assert_local_filesystem(self) -> None:
        """Reject provably non-local filesystems before taking any lock.

        Windows accepts only a fixed local drive, using ``GetDriveTypeW`` so a
        mapped drive is not mistaken for a local drive letter. Linux accepts an
        explicit local-filesystem allowlist. Unknown or remote storage fails
        closed before the receipt root or lock anchor is created; the exclusion
        self-test is an additional capability check, not a filesystem classifier.
        """
        if os.name == "nt":
            drive_type = self._windows_drive_type(self.root)
            if drive_type == _WINDOWS_DRIVE_REMOTE:
                raise ReceiptStoreUnavailable(
                    "receipt store on a UNC/remote volume is not supported"
                )
            if drive_type != _WINDOWS_DRIVE_FIXED:
                raise ReceiptStoreUnavailable(
                    f"receipt store is not on a fixed local drive: type={drive_type}"
                )
            return
        self._assert_supported_linux_filesystem(self._linux_mount_fstype())

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
        if not self._lock_support_verified:
            self._assert_local_filesystem()
        self.root.mkdir(parents=True, exist_ok=True)
        self._assert_no_legacy_guard()
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

    def discover_legacy_guards(self) -> list[Path]:
        """List every legacy ``.guard`` file under the receipt root.

        RCP-02B discovery is read-only: it neither deletes nor mutates the
        guard, and it must work even though every write path fails closed
        while a legacy guard is present.
        """

        guards: list[Path] = []
        if not os.path.isdir(_native_path(self.root)):
            return guards
        for entry in os.scandir(_native_path(self.root)):
            if _LEGACY_GUARD_NAME.fullmatch(entry.name):
                guards.append(self.root / entry.name)
        return sorted(guards, key=lambda path: path.name)

    def reconcile_legacy_guard(
        self,
        guard_path: Path,
        *,
        target_id: str,
        operator_id: str,
        writer_quiescence: GuardWriterQuiescence,
        plan_digest: str | None = None,
        execution_id: str | None = None,
        now: datetime | None = None,
    ) -> ReceiptGuardReconciliation:
        """Explicitly reconcile one legacy guard (RCP-02B frozen order).

        The frozen order (contract §8) is: verify the guard facts, take the
        new advisory lock for the guard's scope, publish the content-addressed
        reconciliation evidence, then delete the guard, then re-verify the
        chain.  Evidence always precedes deletion so the crash seam
        "guard deleted, attention not cleared" stays recoverable from disk.

        GAP-R02B-1: the advisory lock taken here guards only the brief scan
        window; a genuinely still-running legacy writer holding the old
        ``O_EXCL`` guard would not contend with this lock at all.  The real
        protection is the operator's ``writer_quiescence`` declaration plus
        the deployment hard gate that stopped legacy writers before
        RCP-02A ever ran.
        """

        recorded_at = now if now is not None else utc_now()
        guard_name = guard_path.name
        if not _LEGACY_GUARD_NAME.fullmatch(guard_name):
            raise ReceiptStoreError(f"not a legacy receipt guard: {guard_name}")
        identity_hex, _, operation_text = guard_name[1:].removesuffix(".guard").partition(".")
        operation = ReceiptOperation(operation_text)
        execution_digest = f"sha256:{identity_hex}"

        if plan_digest is not None and execution_id is not None:
            recomputed = self._execution_digest(plan_digest, execution_id)
            if recomputed != execution_digest:
                raise ReceiptStoreError(
                    "guard identity does not match the supplied plan/execution"
                )
            plan_for_lock: str | None = plan_digest
            execution_for_lock: str | None = execution_id
        else:
            plan_for_lock = None
            execution_for_lock = None

        if not writer_quiescence.declared:
            raise ReceiptStoreError(
                "writer quiescence must be declared before guard reconciliation"
            )

        guard_bytes = b""
        try:
            with open(_native_path(guard_path), "rb") as stream:
                guard_bytes = stream.read()
        except OSError as error:
            raise ReceiptStoreError(f"cannot read legacy guard: {guard_name}") from error
        guard_sha256 = "sha256:" + hashlib.sha256(guard_bytes).hexdigest()
        discovered_at = datetime.fromtimestamp(
            guard_path.stat().st_mtime, tz=UTC
        )

        chain_head_digest: str | None = None
        outcome = GuardReconciliationOutcome.ORPHAN_CONFIRMED
        reason = "guard holds no receipt content and the chain verifies without it"
        guard_present_before = os.path.exists(_native_path(guard_path))
        if not guard_present_before:
            raise ReceiptStoreError(f"legacy guard disappeared: {guard_name}")

        # Recovery lock entry: the scope's chain (if any) is verified under the
        # new advisory lock.  A legacy guard cannot be held by this lock, but
        # the lock still serializes this reconciliation against new-version
        # writers for the same scope.
        with self._recovery_mutex(execution_digest, plan_for_lock, execution_for_lock):
            if plan_for_lock is not None:
                try:
                    head = self.head(plan_for_lock, execution_for_lock, operation)
                    chain_head_digest = head.digest if head is not None else None
                except ReceiptStoreError as error:
                    outcome = GuardReconciliationOutcome.NEEDS_ATTENTION
                    reason = f"receipt chain verification failed: {error}"
            else:
                chain_head_digest = self._find_head_by_execution_digest(
                    execution_digest, operation
                )

            evidence = ReceiptGuardReconciliation(
                guard_filename=guard_name,
                execution_digest=execution_digest,
                operation=operation,
                guard_sha256=guard_sha256,
                receipt_root=str(self.root.resolve()),
                discovered_at=discovered_at,
                target_id=target_id,
                operator_id=operator_id,
                writer_quiescence=writer_quiescence,
                chain_head_digest=chain_head_digest,
                outcome=outcome,
                reason=reason,
                recorded_at=recorded_at,
            )
            self._publish_guard_reconciliation(evidence)

            if outcome is GuardReconciliationOutcome.ORPHAN_CONFIRMED:
                os.unlink(_native_path(guard_path))
                # Post-deletion chain re-verification (frozen step 7): when a
                # chain exists it must still verify; an absent chain is the
                # legitimate orphan-guard case (head() returning None above).
                if plan_for_lock is not None and chain_head_digest is not None:
                    self.verify_chain(plan_for_lock, execution_for_lock, operation)
        return evidence

    def _find_head_by_execution_digest(
        self, execution_digest: str, operation: ReceiptOperation
    ) -> str | None:
        """Best-effort head lookup when plan/execution were not supplied.

        GAP-R02B-2: without plan/execution the association is only via the
        pointer filename.  When no pointer matches, the evidence records a
        null head and the caller decides whether that is acceptable; it is
        not a chain-integrity claim.
        """

        pointer_name = (
            f"{execution_digest.removeprefix('sha256:')}.{operation.value}.current.json"
        )
        pointer_path = self.root / pointer_name
        pointer = self._read_pointer(pointer_path)
        if pointer is None:
            return None
        return pointer.receipt_digest

    def _publish_guard_reconciliation(
        self, evidence: ReceiptGuardReconciliation
    ) -> None:
        """Durably publish reconciliation evidence next to the receipt root.

        The evidence file is content-addressed by the model digest, so a crash
        between publish and guard deletion is idempotently retryable: the same
        reconciliation writes the same file again.
        """

        self.root.mkdir(parents=True, exist_ok=True)
        evidence_dir = self.root / "guard-reconciliations"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        path = evidence_dir / f"{evidence.digest.removeprefix('sha256:')}.json"
        if os.path.exists(_native_path(path)):
            with open(_native_path(path), encoding="utf-8") as stream:
                existing = json.loads(stream.read())
            if existing != evidence.model_dump(mode="json", exclude_none=False):
                raise ReceiptStoreError(
                    "existing guard reconciliation evidence is not idempotent"
                )
        else:
            self._atomic_write(path, evidence.model_dump(mode="json", exclude_none=False))

    @contextmanager
    def _recovery_mutex(
        self,
        execution_digest: str,
        plan_digest: str | None,
        execution_id: str | None,
    ) -> Iterator[None]:
        """Advisory lock for the reconciliation scope.

        When plan/execution are known, this is exactly the scope lock of
        ``_mutex``.  When they are unknown, the lock anchors on the
        execution-digest identity itself, which still serializes concurrent
        reconciliations of the same guard file.  Neither path mutates the
        store, so ``_assert_no_legacy_guard`` must not run here.
        """

        if plan_digest is not None and execution_id is not None:
            identity = self._execution_digest(plan_digest, execution_id).removeprefix(
                "sha256:"
            )
        else:
            identity = execution_digest.removeprefix("sha256:")
        lock_path = self.root / f".{identity}.reconcile.lock"
        fd = self._acquire_lock(lock_path)
        try:
            yield
        finally:
            try:
                _unlock(fd)
            finally:
                os.close(fd)

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
    "GuardReconciliationOutcome",
    "GuardWriterQuiescence",
    "InterventionReceiptPointer",
    "RECEIPT_POINTER_SCHEMA",
    "ReceiptGuardReconciliation",
    "ReceiptStoreError",
    "ReceiptStoreUnavailable",
]
