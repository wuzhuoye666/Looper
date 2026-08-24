"""L7 负缓存：登记并查询"未能优化到的指标/候选"。

架构层：总体架构 v2 的 L7（见 docs/system-optimizer/architecture/overall.md）。
缓存的是证据不是结论：每条记录必须挂至少一个证据 digest；身份四分量
（环境 × 候选参数 × 压力协议 × 公式版本）任一变化即视为不同键，不设跨环境信任。
存储 JSONL 追加式（append-only），坏行报错不跳过。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.config_manifest import ConfigComponent

NEGATIVE_CACHE_SCHEMA = "looper.negative-cache-entry/v1alpha1"
HYPOTHESIS_NEGATIVE_CACHE_SCHEMA = "looper.hypothesis-negative-cache-entry/v1alpha1"
HYPOTHESIS_RETENTION_POLICY_SCHEMA = "looper.hypothesis-cache-retention/v1alpha1"
HYPOTHESIS_SEMANTICS_VERSION = "looper.hypothesis-semantics/v1alpha1"
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"


class NegativeVerdict(StrEnum):
    # Verdicts describe candidate effectiveness only. Measurement-quality failures
    # (e.g. the S1.1 CV stability gate) are not candidate verdicts and are never
    # cached here: caching them would conflate transient noise with a bad candidate.
    NO_IMPROVEMENT_LCB = "no-improvement-lcb"
    GATE_REJECTED = "gate-rejected"


class NegativeCacheIdentity(StrictModel):
    environment_digest: str = Field(pattern=_DIGEST_PATTERN)
    candidate_parameters_digest: str = Field(pattern=_DIGEST_PATTERN)
    pressure_protocol_digest: str = Field(pattern=_DIGEST_PATTERN)
    formula_versions_digest: str = Field(pattern=_DIGEST_PATTERN)

    @property
    def key(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class NegativeCacheEntry(StrictModel):
    schema_version: Literal[NEGATIVE_CACHE_SCHEMA] = NEGATIVE_CACHE_SCHEMA
    identity: NegativeCacheIdentity
    metric_id: str = Field(min_length=1, max_length=160)
    verdict: NegativeVerdict
    evidence_digests: list[str] = Field(min_length=1)
    detail: str = Field(min_length=1, max_length=1000)
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_evidence_binding(self) -> NegativeCacheEntry:
        if len(self.evidence_digests) != len(set(self.evidence_digests)):
            raise ValueError("evidence digests must be unique")
        for digest in self.evidence_digests:
            if re.fullmatch(_DIGEST_PATTERN, digest) is None:
                raise ValueError(
                    "evidence digests must be strict lowercase sha256 references"
                )
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class HypothesisRetentionMode(StrEnum):
    IDENTITY_CHANGE_ONLY = "identity-change-only"
    EXPIRE_AT = "expire-at"


class HypothesisCacheRetentionPolicy(StrictModel):
    """Explicit lifecycle input; absence never implies permanent retention."""

    schema_version: Literal[HYPOTHESIS_RETENTION_POLICY_SCHEMA] = (
        HYPOTHESIS_RETENTION_POLICY_SCHEMA
    )
    policy_id: str = Field(min_length=1, max_length=160)
    mode: HypothesisRetentionMode
    expires_at: datetime | None

    @model_validator(mode="after")
    def validate_expiration(self) -> HypothesisCacheRetentionPolicy:
        if self.mode is HypothesisRetentionMode.EXPIRE_AT:
            if self.expires_at is None:
                raise ValueError("expire-at retention requires expires_at")
            if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
                raise ValueError("retention expires_at must be timezone-aware")
        elif self.expires_at is not None:
            raise ValueError("identity-change-only retention cannot carry expires_at")
        return self

    def active_at(self, at: datetime) -> bool:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("hypothesis cache lookup time must be timezone-aware")
        return self.expires_at is None or at < self.expires_at

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class HypothesisNegativeCacheIdentity(StrictModel):
    environment_digest: str = Field(pattern=_DIGEST_PATTERN)
    workload_identity_digest: str = Field(pattern=_DIGEST_PATTERN)
    component: ConfigComponent
    symptom_class_digest: str = Field(pattern=_DIGEST_PATTERN)
    metric_contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    refutation_policy_digest: str = Field(pattern=_DIGEST_PATTERN)
    formula_versions_digest: str = Field(pattern=_DIGEST_PATTERN)
    hypothesis_semantics_version: Literal[HYPOTHESIS_SEMANTICS_VERSION] = (
        HYPOTHESIS_SEMANTICS_VERSION
    )

    @property
    def key(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class HypothesisNegativeCacheEntry(StrictModel):
    """A business-retest refutation bound to its complete comparison identity."""

    schema_version: Literal[HYPOTHESIS_NEGATIVE_CACHE_SCHEMA] = (
        HYPOTHESIS_NEGATIVE_CACHE_SCHEMA
    )
    identity: HypothesisNegativeCacheIdentity
    evidence_digests: list[str] = Field(min_length=1)
    detail: str = Field(min_length=1, max_length=1000)
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_evidence_binding(self) -> HypothesisNegativeCacheEntry:
        if len(self.evidence_digests) != len(set(self.evidence_digests)):
            raise ValueError("evidence digests must be unique")
        for digest in self.evidence_digests:
            if re.fullmatch(_DIGEST_PATTERN, digest) is None:
                raise ValueError(
                    "evidence digests must be strict lowercase sha256 references"
                )
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("hypothesis cache recorded_at must be timezone-aware")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


NegativeCacheRecord = NegativeCacheEntry | HypothesisNegativeCacheEntry


def candidate_parameters_digest(candidate_parameters: Mapping[str, object]) -> str:
    return canonical_digest(dict(candidate_parameters))


def formula_versions_digest(formula_versions: Mapping[str, str]) -> str:
    if not formula_versions:
        raise ValueError("formula_versions must not be empty")
    return canonical_digest(dict(formula_versions))


def _entry_line(entry: NegativeCacheRecord) -> str:
    return json.dumps(entry.model_dump(mode="json"), sort_keys=True) + "\n"


def _atomic_replace_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    descriptor_open = True
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor_open = False
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor_open:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _atomic_write_snapshot(path: Path, entries: Sequence[NegativeCacheRecord]) -> None:
    """Replace a JSONL snapshot atomically after its bytes reach the file handle."""

    payload = "".join(_entry_line(entry) for entry in entries).encode("utf-8")
    _atomic_replace_bytes(path, payload)


class NegativeCache:
    """Append-only negative-result registry consulted by the L8 scheduler."""

    def __init__(self, entries: Sequence[NegativeCacheRecord] | None = None) -> None:
        self._records: list[NegativeCacheRecord] = list(entries or [])
        self._entries = [
            entry for entry in self._records if isinstance(entry, NegativeCacheEntry)
        ]
        self._hypothesis_entries = [
            entry
            for entry in self._records
            if isinstance(entry, HypothesisNegativeCacheEntry)
        ]
        self._by_key: dict[str, list[NegativeCacheEntry]] = {}
        for entry in self._entries:
            self._by_key.setdefault(entry.identity.key, []).append(entry)
        self._hypothesis_by_key: dict[str, list[HypothesisNegativeCacheEntry]] = {}
        for entry in self._hypothesis_entries:
            self._hypothesis_by_key.setdefault(entry.identity.key, []).append(entry)

    def __len__(self) -> int:
        return len(self._records)

    @property
    def entries(self) -> list[NegativeCacheEntry]:
        return list(self._entries)

    @property
    def hypothesis_entries(self) -> list[HypothesisNegativeCacheEntry]:
        return list(self._hypothesis_entries)

    @property
    def records(self) -> list[NegativeCacheRecord]:
        return list(self._records)

    def add(self, entry: NegativeCacheEntry) -> None:
        self._records.append(entry)
        self._entries.append(entry)
        self._by_key.setdefault(entry.identity.key, []).append(entry)

    def add_hypothesis(self, entry: HypothesisNegativeCacheEntry) -> None:
        self._records.append(entry)
        self._hypothesis_entries.append(entry)
        self._hypothesis_by_key.setdefault(entry.identity.key, []).append(entry)

    def lookup_key(self, key: str) -> list[NegativeCacheEntry]:
        return list(self._by_key.get(key, []))

    def lookup(
        self,
        *,
        environment_digest: str,
        candidate_parameters: Mapping[str, object],
        pressure_protocol_digest: str,
        formula_versions: Mapping[str, str],
    ) -> list[NegativeCacheEntry]:
        identity = NegativeCacheIdentity(
            environment_digest=environment_digest,
            candidate_parameters_digest=candidate_parameters_digest(candidate_parameters),
            pressure_protocol_digest=pressure_protocol_digest,
            formula_versions_digest=formula_versions_digest(formula_versions),
        )
        return self.lookup_key(identity.key)

    def lookup_hypothesis(
        self,
        identity: HypothesisNegativeCacheIdentity,
        *,
        retention_policy: HypothesisCacheRetentionPolicy,
        at: datetime,
    ) -> list[HypothesisNegativeCacheEntry]:
        if not retention_policy.active_at(at):
            return []
        return list(self._hypothesis_by_key.get(identity.key, []))

    def dump(self, path: Path) -> None:
        """Atomically publish the full current state as a fresh snapshot."""

        _atomic_write_snapshot(path, self._records)

    def append_to(self, path: Path, entry: NegativeCacheRecord) -> None:
        """Atomically append one logical line, then expose it through memory lookup.

        The replacement preserves every existing byte and adds exactly one JSONL
        record. A failed write/replace leaves both the prior file and the in-memory
        index unchanged instead of publishing a partial trailing line.
        """

        existing = path.read_bytes() if path.exists() else b""
        if existing and not existing.endswith(b"\n"):
            raise ValueError(f"negative cache JSONL has a truncated final line: {path}")
        payload = existing + _entry_line(entry).encode("utf-8")
        _atomic_replace_bytes(path, payload)
        if isinstance(entry, NegativeCacheEntry):
            self.add(entry)
        else:
            self.add_hypothesis(entry)

    @classmethod
    def load(cls, path: Path) -> NegativeCache:
        entries: list[NegativeCacheRecord] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    if not isinstance(payload, dict):
                        raise ValueError("negative cache line must contain an object")
                    schema_version = payload.get("schema_version")
                    if schema_version == NEGATIVE_CACHE_SCHEMA:
                        entries.append(NegativeCacheEntry.model_validate(payload))
                    elif schema_version == HYPOTHESIS_NEGATIVE_CACHE_SCHEMA:
                        entries.append(HypothesisNegativeCacheEntry.model_validate(payload))
                    else:
                        raise ValueError(
                            f"unsupported negative cache schema: {schema_version!r}"
                        )
                except Exception as error:
                    raise ValueError(
                        f"invalid negative cache entry at {path}:{line_number}: {error}"
                    ) from error
        return cls(entries)


__all__ = [
    "HYPOTHESIS_NEGATIVE_CACHE_SCHEMA",
    "HYPOTHESIS_RETENTION_POLICY_SCHEMA",
    "HYPOTHESIS_SEMANTICS_VERSION",
    "HypothesisCacheRetentionPolicy",
    "HypothesisNegativeCacheEntry",
    "HypothesisNegativeCacheIdentity",
    "HypothesisRetentionMode",
    "NegativeCache",
    "NegativeCacheEntry",
    "NegativeCacheIdentity",
    "NegativeVerdict",
    "candidate_parameters_digest",
    "formula_versions_digest",
]
