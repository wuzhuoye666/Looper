"""Durable, content-addressed journals for D5 two-phase interventions.

The store proves internal chain integrity and execution association. It is not a
truth anchor and never replays backend writes; a non-terminal head is evidence
for the existing lease/state reconciliation path.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

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


class ReceiptStoreError(RuntimeError):
    """Raised when a durable receipt chain cannot be trusted."""


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

    @contextmanager
    def _mutex(
        self, plan_digest: str, execution_id: str, operation: ReceiptOperation
    ) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        identity = self._execution_digest(plan_digest, execution_id).removeprefix("sha256:")
        path = self.root / f".{identity}.{operation.value}.guard"
        try:
            descriptor = os.open(_native_path(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise ReceiptStoreError("receipt chain is busy") from error
        try:
            os.close(descriptor)
            yield
        finally:
            if os.path.exists(_native_path(path)):
                os.unlink(_native_path(path))

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
