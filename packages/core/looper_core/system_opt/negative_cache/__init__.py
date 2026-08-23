"""L7 负缓存：登记并查询"未能优化到的指标/候选"。

架构层：总体架构 v2 的 L7（见 docs/system-optimizer/architecture/overall.md）。
缓存的是证据不是结论：每条记录必须挂至少一个证据 digest；身份四分量
（环境 × 候选参数 × 压力协议 × 公式版本）任一变化即视为不同键，不设跨环境信任。
存储 JSONL 追加式（append-only），坏行报错不跳过。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel

NEGATIVE_CACHE_SCHEMA = "looper.negative-cache-entry/v1alpha1"
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
            if not digest.startswith("sha256:"):
                raise ValueError("evidence digests must be sha256 references")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


def candidate_parameters_digest(candidate_parameters: Mapping[str, object]) -> str:
    return canonical_digest(dict(candidate_parameters))


def formula_versions_digest(formula_versions: Mapping[str, str]) -> str:
    if not formula_versions:
        raise ValueError("formula_versions must not be empty")
    return canonical_digest(dict(formula_versions))


class NegativeCache:
    """Append-only negative-result registry consulted by the L8 scheduler."""

    def __init__(self, entries: Sequence[NegativeCacheEntry] | None = None) -> None:
        self._entries: list[NegativeCacheEntry] = list(entries or [])
        self._by_key: dict[str, list[NegativeCacheEntry]] = {}
        for entry in self._entries:
            self._by_key.setdefault(entry.identity.key, []).append(entry)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> list[NegativeCacheEntry]:
        return list(self._entries)

    def add(self, entry: NegativeCacheEntry) -> None:
        self._entries.append(entry)
        self._by_key.setdefault(entry.identity.key, []).append(entry)

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

    def dump(self, path: Path) -> None:
        """Write the full current state as a fresh snapshot (overwrites)."""

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for entry in self._entries:
                handle.write(json.dumps(entry.model_dump(mode="json"), sort_keys=True) + "\n")

    def append_to(self, path: Path, entry: NegativeCacheEntry) -> None:
        self.add(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry.model_dump(mode="json"), sort_keys=True) + "\n")

    @classmethod
    def load(cls, path: Path) -> NegativeCache:
        entries: list[NegativeCacheEntry] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    entries.append(NegativeCacheEntry.model_validate(json.loads(line)))
                except Exception as error:
                    raise ValueError(
                        f"invalid negative cache entry at {path}:{line_number}: {error}"
                    ) from error
        return cls(entries)


__all__ = [
    "NegativeCache",
    "NegativeCacheEntry",
    "NegativeCacheIdentity",
    "NegativeVerdict",
    "candidate_parameters_digest",
    "formula_versions_digest",
]
