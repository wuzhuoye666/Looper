"""L1 状态与所有权证据：manifest → 当前状态证据 → 所有权/授权声明。

架构层：L1（docs/system-optimizer/architecture/overall.md）。
状态证据校验当前环境与 digest，所有权声明决定哪些项可被自动修改；
证据缺失或身份不一致时 fail-closed。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator

from looper_core.canonical import canonical_digest, canonical_json
from looper_core.contracts import StrictModel
from looper_core.system_opt.config_manifest import ConfigManifest
from looper_core.system_opt.executor.local_linux import parse_readback

STATE_EVIDENCE_SCHEMA = "looper.system-config-state-evidence/v1alpha1"
OWNERSHIP_DECLARATION_SCHEMA = "looper.system-config-ownership-declaration/v1alpha1"


class StateEvidenceError(ValueError):
    pass


class PersistenceDisposition(StrEnum):
    DECLARED = "declared"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class OwnershipDisposition(StrEnum):
    EXPLICIT = "explicit-owner"
    EXTERNAL = "external-writer"
    UNOWNED = "explicitly-unowned"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class StateSource(StrictModel):
    kind: str = Field(min_length=1, max_length=120)
    locator: str = Field(min_length=1, max_length=2000)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    line: int | None = Field(default=None, ge=1)
    raw_value: str | None = None


class AssignmentObservation(StrictModel):
    key: str = Field(min_length=1, max_length=500)
    value: str
    section: str | None = None
    source: StateSource


class ConfigStateRecord(StrictModel):
    item_id: str = Field(min_length=1, max_length=120)
    parameter_id: str = Field(min_length=1, max_length=200)
    persistence: PersistenceDisposition
    persistent_value: Any | None = None
    ownership: OwnershipDisposition
    owner_id: str | None = Field(default=None, min_length=1, max_length=200)
    pinned: bool
    sources: list[StateSource]
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_evidence(self) -> ConfigStateRecord:
        if self.persistence == PersistenceDisposition.DECLARED:
            if self.persistent_value is None or not self.sources:
                raise ValueError("declared persistence requires a value and source evidence")
        elif self.persistent_value is not None:
            raise ValueError("non-declared persistence cannot carry a persistent value")
        if self.ownership in {
            OwnershipDisposition.EXPLICIT,
            OwnershipDisposition.EXTERNAL,
        }:
            if self.owner_id is None or not self.sources:
                raise ValueError("owned state requires owner_id and source evidence")
        elif self.owner_id is not None:
            raise ValueError("unowned, conflict, or unknown state cannot carry owner_id")
        if self.pinned and self.ownership not in {
            OwnershipDisposition.EXPLICIT,
            OwnershipDisposition.EXTERNAL,
        }:
            raise ValueError("pinned state requires an identified owner")
        return self


class OwnershipDeclaration(StrictModel):
    schema_version: str
    target_id: str = Field(min_length=1, max_length=200)
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    environment_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_evidence_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    actor_id: str = Field(min_length=1, max_length=200)
    declared_by: str = Field(min_length=1, max_length=200)
    item_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=2000)
    declared_at: datetime

    @field_validator("schema_version")
    @classmethod
    def validate_schema(cls, value: str) -> str:
        if value != OWNERSHIP_DECLARATION_SCHEMA:
            raise ValueError(f"schema_version must be {OWNERSHIP_DECLARATION_SCHEMA!r}")
        return value

    @model_validator(mode="after")
    def validate_declaration(self) -> OwnershipDeclaration:
        if self.declared_at.tzinfo is None:
            raise ValueError("ownership declaration timestamp must be timezone-aware")
        if len(self.item_ids) != len(set(self.item_ids)):
            raise ValueError("ownership declaration item ids must be unique")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class ConfigurationStateEvidence(StrictModel):
    schema_version: str
    target_id: str = Field(min_length=1, max_length=200)
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    environment_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    collected_at: datetime
    source_scope: list[str] = Field(min_length=1)
    assignments: list[AssignmentObservation]
    records: list[ConfigStateRecord]
    ownership_declarations: list[OwnershipDeclaration] = Field(default_factory=list)
    counting_basis: str = Field(min_length=1, max_length=2000)

    @field_validator("schema_version")
    @classmethod
    def validate_schema(cls, value: str) -> str:
        if value != STATE_EVIDENCE_SCHEMA:
            raise ValueError(f"schema_version must be {STATE_EVIDENCE_SCHEMA!r}")
        return value

    @model_validator(mode="after")
    def validate_unique_records(self) -> ConfigurationStateEvidence:
        ids = [record.item_id for record in self.records]
        parameters = [record.parameter_id for record in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("state evidence item ids must be unique")
        if len(parameters) != len(set(parameters)):
            raise ValueError("state evidence parameter ids must be unique")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))

    def validate_identity(
        self,
        manifest: ConfigManifest,
        target_id: str,
        *,
        environment_digest: str | None = None,
    ) -> None:
        if self.target_id != target_id:
            raise StateEvidenceError("state evidence target_id does not match the target")
        if self.manifest_digest != manifest.digest:
            raise StateEvidenceError("state evidence manifest digest does not match")
        if environment_digest is not None and self.environment_digest != environment_digest:
            raise StateEvidenceError(
                "state evidence environment digest does not match the current host"
            )
        expected = {item.id: item.parameter_id for item in manifest.items}
        for record in self.records:
            if expected.get(record.item_id) != record.parameter_id:
                raise StateEvidenceError(f"state evidence identity mismatch for {record.item_id!r}")

    def records_by_item(self) -> dict[str, ConfigStateRecord]:
        return {record.item_id: record for record in self.records}

    def apply_ownership_declaration(
        self, declaration: OwnershipDeclaration
    ) -> ConfigurationStateEvidence:
        if declaration.target_id != self.target_id:
            raise StateEvidenceError("ownership declaration target does not match")
        if declaration.manifest_digest != self.manifest_digest:
            raise StateEvidenceError("ownership declaration manifest does not match")
        if declaration.environment_digest != self.environment_digest:
            raise StateEvidenceError("ownership declaration environment does not match")
        if declaration.source_evidence_digest != self.digest:
            raise StateEvidenceError("ownership declaration does not bind source evidence")
        selected = set(declaration.item_ids)
        by_item = self.records_by_item()
        missing = sorted(selected - set(by_item))
        if missing:
            raise StateEvidenceError(
                f"ownership declaration references unknown item ids: {missing}"
            )
        locator = f"operator-declaration:{declaration.digest}"
        declaration_source = StateSource(
            kind="operator-ownership-declaration",
            locator=locator,
            content_sha256=declaration.digest.removeprefix("sha256:"),
        )
        records: list[ConfigStateRecord] = []
        for record in self.records:
            if record.item_id not in selected:
                records.append(record)
                continue
            records.append(
                record.model_copy(
                    update={
                        "ownership": OwnershipDisposition.EXPLICIT,
                        "owner_id": declaration.actor_id,
                        "pinned": False,
                        "sources": [*record.sources, declaration_source],
                        "reason": (
                            f"operator {declaration.declared_by!r} explicitly assigned "
                            f"runtime write ownership to {declaration.actor_id!r}: "
                            f"{declaration.reason}"
                        ),
                    }
                )
            )
        return self.model_copy(
            update={
                "source_scope": [*self.source_scope, locator],
                "records": records,
                "ownership_declarations": [
                    *self.ownership_declarations,
                    declaration,
                ],
            }
        )

    def safety_constraints(
        self,
        manifest: ConfigManifest,
        *,
        target_id: str,
        actor_id: str,
        environment_digest: str | None = None,
    ) -> tuple[set[str], set[str]]:
        self.validate_identity(
            manifest,
            target_id,
            environment_digest=environment_digest,
        )
        by_item = self.records_by_item()
        pinned: set[str] = set()
        blocked: set[str] = set()
        for item in manifest.items:
            record = by_item.get(item.id)
            if record is None:
                blocked.add(item.id)
                continue
            if record.pinned:
                pinned.add(item.id)
                continue
            if record.ownership == OwnershipDisposition.UNOWNED:
                continue
            if record.ownership == OwnershipDisposition.EXPLICIT and record.owner_id == actor_id:
                continue
            blocked.add(item.id)
        return pinned, blocked


_ASSIGNMENT = re.compile(r"^\s*([A-Za-z0-9_.\-/]+)\s*=\s*(.*?)\s*$")
_SECTION = re.compile(r"^\s*\[([^]]+)]\s*$")


class LinuxExactAssignmentCollector:
    def collect(
        self,
        manifest: ConfigManifest,
        *,
        target_id: str,
        environment_digest: str,
        source_paths: list[Path],
        collected_at: datetime,
    ) -> ConfigurationStateEvidence:
        if collected_at.tzinfo is None:
            raise StateEvidenceError("collected_at must be timezone-aware")
        if not source_paths:
            raise StateEvidenceError("at least one explicit state source is required")
        assignments: list[AssignmentObservation] = []
        resolved: list[str] = []
        for path in source_paths:
            selected = path.resolve()
            resolved.append(str(selected))
            try:
                payload = selected.read_bytes()
            except OSError as error:
                raise StateEvidenceError(f"cannot read state source {selected}: {error}") from error
            digest = hashlib.sha256(payload).hexdigest()
            section: str | None = None
            for line_number, line in enumerate(
                payload.decode("utf-8", errors="replace").splitlines(), start=1
            ):
                stripped = line.strip()
                if not stripped or stripped.startswith(("#", ";")):
                    continue
                section_match = _SECTION.match(line)
                if section_match:
                    section = section_match.group(1)
                    continue
                match = _ASSIGNMENT.match(line)
                if not match:
                    continue
                key, value = match.groups()
                assignments.append(
                    AssignmentObservation(
                        key=key,
                        value=value,
                        section=section,
                        source=StateSource(
                            kind=(
                                "tuned-profile"
                                if "tuned" in selected.parts
                                else "exact-assignment-file"
                            ),
                            locator=str(selected),
                            content_sha256=digest,
                            line=line_number,
                            raw_value=value,
                        ),
                    )
                )
        records = [self._record(item, assignments) for item in manifest.items]
        return ConfigurationStateEvidence(
            schema_version=STATE_EVIDENCE_SCHEMA,
            target_id=target_id,
            manifest_digest=manifest.digest,
            environment_digest=environment_digest,
            collected_at=collected_at,
            source_scope=resolved,
            assignments=assignments,
            records=records,
            ownership_declarations=[],
            counting_basis=(
                "all parseable key=value assignments from every explicitly listed source "
                "are retained; exact target or manifest-declared persistent-key matching "
                "only; no precedence inference"
            ),
        )

    @staticmethod
    def _record(item: Any, assignments: list[AssignmentObservation]) -> ConfigStateRecord:
        exact_keys = {item.target, *item.persistent_keys}
        matches = [assignment for assignment in assignments if assignment.key in exact_keys]
        if not matches:
            return ConfigStateRecord(
                item_id=item.id,
                parameter_id=item.parameter_id,
                persistence=PersistenceDisposition.UNKNOWN,
                persistent_value=None,
                ownership=OwnershipDisposition.UNKNOWN,
                owner_id=None,
                pinned=False,
                sources=[],
                reason=(
                    "no exact target or manifest-declared persistent-key assignment was "
                    "found in the explicit source scope"
                ),
            )
        parsed: list[Any] = []
        try:
            for match in matches:
                parsed.append(parse_readback(item, match.value))
        except ValueError as error:
            return ConfigStateRecord(
                item_id=item.id,
                parameter_id=item.parameter_id,
                persistence=PersistenceDisposition.CONFLICT,
                persistent_value=None,
                ownership=OwnershipDisposition.CONFLICT,
                owner_id=None,
                pinned=False,
                sources=[match.source for match in matches],
                reason=f"an exact assignment could not be parsed: {error}",
            )
        distinct = {canonical_json(value) for value in parsed}
        if len(matches) != 1 or len(distinct) != 1:
            return ConfigStateRecord(
                item_id=item.id,
                parameter_id=item.parameter_id,
                persistence=(
                    PersistenceDisposition.DECLARED
                    if len(distinct) == 1
                    else PersistenceDisposition.CONFLICT
                ),
                persistent_value=parsed[0] if len(distinct) == 1 else None,
                ownership=OwnershipDisposition.CONFLICT,
                owner_id=None,
                pinned=False,
                sources=[match.source for match in matches],
                reason=(
                    "multiple exact assignments exist; precedence and ownership are not inferred"
                ),
            )
        return ConfigStateRecord(
            item_id=item.id,
            parameter_id=item.parameter_id,
            persistence=PersistenceDisposition.DECLARED,
            persistent_value=parsed[0],
            ownership=OwnershipDisposition.EXTERNAL,
            owner_id=f"file:{matches[0].source.locator}",
            pinned=False,
            sources=[matches[0].source],
            reason="one exact assignment was observed; external ownership blocks automatic writes",
        )


__all__ = [
    "AssignmentObservation",
    "ConfigStateRecord",
    "ConfigurationStateEvidence",
    "LinuxExactAssignmentCollector",
    "OWNERSHIP_DECLARATION_SCHEMA",
    "OwnershipDeclaration",
    "OwnershipDisposition",
    "PersistenceDisposition",
    "STATE_EVIDENCE_SCHEMA",
    "StateEvidenceError",
    "StateSource",
]
