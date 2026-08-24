from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from looper_core.system_opt.negative_cache import (
    HYPOTHESIS_SEMANTICS_VERSION,
    HypothesisCacheRetentionPolicy,
    HypothesisNegativeCacheEntry,
    HypothesisNegativeCacheIdentity,
    NegativeCache,
    NegativeCacheEntry,
    NegativeCacheIdentity,
    NegativeVerdict,
)

DIGESTS = [f"sha256:{value:064x}" for value in range(1, 16)]
NOW = datetime(2026, 8, 24, tzinfo=UTC)


def _identity(**updates: object) -> HypothesisNegativeCacheIdentity:
    payload: dict[str, object] = {
        "environment_digest": DIGESTS[0],
        "workload_identity_digest": DIGESTS[1],
        "component": "cpu",
        "symptom_class_digest": DIGESTS[2],
        "metric_contract_digest": DIGESTS[3],
        "refutation_policy_digest": DIGESTS[4],
        "formula_versions_digest": DIGESTS[5],
        "hypothesis_semantics_version": HYPOTHESIS_SEMANTICS_VERSION,
    }
    payload.update(updates)
    return HypothesisNegativeCacheIdentity.model_validate(payload)


def _retention(*, expires_at: datetime | None = None) -> HypothesisCacheRetentionPolicy:
    return HypothesisCacheRetentionPolicy(
        policy_id="demo-retention",
        mode="expire-at" if expires_at is not None else "identity-change-only",
        expires_at=expires_at,
    )


def _entry(identity: HypothesisNegativeCacheIdentity) -> HypothesisNegativeCacheEntry:
    return HypothesisNegativeCacheEntry(
        identity=identity,
        evidence_digests=[DIGESTS[6]],
        detail="comparable stable business retest did not improve",
        recorded_at=NOW,
    )


def test_candidate_and_hypothesis_entries_round_trip_in_one_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "negative-cache.jsonl"
    retention = _retention()
    hypothesis = _entry(_identity())
    candidate = NegativeCacheEntry(
        identity=NegativeCacheIdentity(
            environment_digest=DIGESTS[0],
            candidate_parameters_digest=DIGESTS[7],
            pressure_protocol_digest=DIGESTS[8],
            formula_versions_digest=DIGESTS[9],
        ),
        metric_id="business.score",
        verdict=NegativeVerdict.NO_IMPROVEMENT_LCB,
        evidence_digests=[DIGESTS[10]],
        detail="candidate did not improve",
        recorded_at=NOW,
    )
    cache = NegativeCache()

    cache.append_to(path, candidate)
    cache.append_to(path, hypothesis)
    loaded = NegativeCache.load(path)

    assert loaded.entries == [candidate]
    assert loaded.hypothesis_entries == [hypothesis]
    assert loaded.records == [candidate, hypothesis]
    assert loaded.lookup_hypothesis(_identity(), retention_policy=retention, at=NOW) == [
        hypothesis
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("environment_digest", DIGESTS[11]),
        ("workload_identity_digest", DIGESTS[11]),
        ("component", "memory"),
        ("symptom_class_digest", DIGESTS[11]),
        ("metric_contract_digest", DIGESTS[11]),
        ("refutation_policy_digest", DIGESTS[11]),
        ("formula_versions_digest", DIGESTS[11]),
    ],
)
def test_any_hypothesis_identity_change_misses(field: str, value: object) -> None:
    retention = _retention()
    cache = NegativeCache([_entry(_identity())])

    assert cache.lookup_hypothesis(
        _identity(**{field: value}), retention_policy=retention, at=NOW
    ) == []


def test_explicit_retention_policy_controls_visibility() -> None:
    retention = _retention(expires_at=NOW + timedelta(hours=1))
    cache = NegativeCache([_entry(_identity())])

    assert cache.lookup_hypothesis(_identity(), retention_policy=retention, at=NOW)
    assert not cache.lookup_hypothesis(
        _identity(), retention_policy=retention, at=NOW + timedelta(hours=2)
    )
    assert cache.lookup_hypothesis(_identity(), retention_policy=_retention(), at=NOW)


def test_malformed_or_unknown_mixed_line_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "negative-cache.jsonl"
    path.write_text('{"schema_version":"unknown"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported negative cache schema"):
        NegativeCache.load(path)


def test_hypothesis_entry_rejects_duplicate_or_malformed_evidence() -> None:
    with pytest.raises(ValueError, match="unique"):
        HypothesisNegativeCacheEntry(
            identity=_identity(),
            evidence_digests=[DIGESTS[6], DIGESTS[6]],
            detail="duplicate",
            recorded_at=NOW,
        )
    with pytest.raises(ValueError, match="lowercase"):
        HypothesisNegativeCacheEntry(
            identity=_identity(),
            evidence_digests=["sha256:" + "A" * 64],
            detail="malformed",
            recorded_at=NOW,
        )
