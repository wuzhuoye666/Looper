from __future__ import annotations

import json
from datetime import UTC, datetime

import looper_core.system_opt.negative_cache as cache_module
import pytest
from looper_core.system_opt.negative_cache import (
    NegativeCache,
    NegativeCacheEntry,
    NegativeCacheIdentity,
    NegativeVerdict,
    candidate_parameters_digest,
    formula_versions_digest,
)

ENV = "sha256:" + "c" * 64
PROTOCOL = "sha256:" + "d" * 64
RUN = "sha256:" + "e" * 64
FIXED_AT = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
FORMULAS = {"F-PROJECT-S6-S7": "v1", "F-PROJECT-PRESSURE-CV-BOOTSTRAP-UPPER": "v1alpha1"}


def _identity(**overrides) -> NegativeCacheIdentity:
    payload = dict(
        environment_digest=ENV,
        candidate_parameters_digest=candidate_parameters_digest(
            {"system.cpufreq-governor-uniform": "performance"}
        ),
        pressure_protocol_digest=PROTOCOL,
        formula_versions_digest=formula_versions_digest(FORMULAS),
    )
    payload.update(overrides)
    return NegativeCacheIdentity(**payload)


def _entry(**overrides) -> NegativeCacheEntry:
    payload = dict(
        identity=_identity(),
        metric_id="cpu.bogo-ops-per-second",
        verdict=NegativeVerdict.NO_IMPROVEMENT_LCB,
        evidence_digests=[RUN],
        detail="LCB95=-0.0020 <= MDE=0",
        recorded_at=FIXED_AT,
    )
    payload.update(overrides)
    return NegativeCacheEntry(**payload)


class TestIdentitySensitivity:
    def test_same_identity_same_key(self):
        assert _identity().key == _identity().key

    @pytest.mark.parametrize(
        "field",
        ["environment_digest", "candidate_parameters_digest", "pressure_protocol_digest",
         "formula_versions_digest"],
    )
    def test_any_component_change_changes_key(self, field):
        other = "sha256:" + "f" * 64
        assert _identity().key != _identity(**{field: other}).key

    def test_different_parameter_values_have_different_digests(self):
        assert candidate_parameters_digest({"k": "performance"}) != candidate_parameters_digest(
            {"k": "powersave"}
        )

    def test_formula_versions_must_not_be_empty(self):
        with pytest.raises(ValueError, match="must not be empty"):
            formula_versions_digest({})


class TestEvidenceRedLine:
    def test_entry_without_evidence_is_rejected(self):
        with pytest.raises(ValueError):
            _entry(evidence_digests=[])

    def test_duplicate_evidence_is_rejected(self):
        with pytest.raises(ValueError, match="unique"):
            _entry(evidence_digests=[RUN, RUN])

    def test_non_sha256_evidence_is_rejected(self):
        with pytest.raises(ValueError, match="sha256"):
            _entry(evidence_digests=["run-123"])

    @pytest.mark.parametrize(
        "digest",
        [
            "sha256:abc",
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "a" * 65,
        ],
    )
    def test_malformed_sha256_evidence_is_rejected(self, digest):
        with pytest.raises(ValueError, match="strict lowercase sha256"):
            _entry(evidence_digests=[digest])


class TestCacheBehaviour:
    def test_exact_identity_hits(self):
        cache = NegativeCache([_entry()])
        hits = cache.lookup(
            environment_digest=ENV,
            candidate_parameters={"system.cpufreq-governor-uniform": "performance"},
            pressure_protocol_digest=PROTOCOL,
            formula_versions=FORMULAS,
        )
        assert len(hits) == 1
        assert hits[0].verdict is NegativeVerdict.NO_IMPROVEMENT_LCB

    def test_other_environment_misses(self):
        cache = NegativeCache([_entry()])
        assert not cache.lookup(
            environment_digest="sha256:" + "9" * 64,
            candidate_parameters={"system.cpufreq-governor-uniform": "performance"},
            pressure_protocol_digest=PROTOCOL,
            formula_versions=FORMULAS,
        )

    def test_other_candidate_misses(self):
        cache = NegativeCache([_entry()])
        assert not cache.lookup(
            environment_digest=ENV,
            candidate_parameters={"system.cpufreq-governor-uniform": "powersave"},
            pressure_protocol_digest=PROTOCOL,
            formula_versions=FORMULAS,
        )

    def test_lookup_by_key_matches_only_exact_key(self):
        cache = NegativeCache([_entry()])
        assert len(cache.lookup_key(_identity().key)) == 1
        assert not cache.lookup_key("sha256:" + "0" * 64)


class TestJsonlStore:
    def test_append_preserves_old_lines(self, tmp_path):
        path = tmp_path / "negcache.jsonl"
        cache = NegativeCache()
        cache.append_to(path, _entry())
        cache.append_to(path, _entry(metric_id="cpu.other"))
        loaded = NegativeCache.load(path)
        assert len(loaded) == 2
        assert [entry.metric_id for entry in loaded.entries] == [
            "cpu.bogo-ops-per-second",
            "cpu.other",
        ]

    def test_malformed_line_fails_closed(self, tmp_path):
        path = tmp_path / "negcache.jsonl"
        cache = NegativeCache()
        cache.append_to(path, _entry())
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"broken": true_line()}) + "\n")
        with pytest.raises(ValueError, match="invalid negative cache entry"):
            NegativeCache.load(path)

    def test_round_trip_keeps_digest(self, tmp_path):
        path = tmp_path / "negcache.jsonl"
        entry = _entry()
        NegativeCache().append_to(path, entry)
        loaded = NegativeCache.load(path)
        assert loaded.entries[0].digest == entry.digest

    def test_append_exposes_memory_entry_only_after_fsync(self, tmp_path, monkeypatch):
        path = tmp_path / "negcache.jsonl"
        cache = NegativeCache()
        real_fsync = cache_module.os.fsync
        observed_lengths = []

        def observe_fsync(descriptor):
            observed_lengths.append(len(cache))
            real_fsync(descriptor)

        monkeypatch.setattr(cache_module.os, "fsync", observe_fsync)
        cache.append_to(path, _entry())

        assert observed_lengths == [0]
        assert len(cache) == 1
        assert len(NegativeCache.load(path)) == 1

    def test_append_replace_failure_preserves_disk_and_memory(self, tmp_path, monkeypatch):
        path = tmp_path / "negcache.jsonl"
        original = _entry()
        cache = NegativeCache([original])
        cache.dump(path)

        def fail_replace(source, destination):
            raise OSError(f"injected append replace failure for {destination}")

        monkeypatch.setattr(cache_module.os, "replace", fail_replace)
        with pytest.raises(OSError, match="injected append replace failure"):
            cache.append_to(path, _entry(metric_id="replacement"))

        assert [entry.digest for entry in cache.entries] == [original.digest]
        loaded = NegativeCache.load(path)
        assert [entry.digest for entry in loaded.entries] == [original.digest]
        assert not list(tmp_path.glob(".negcache.jsonl.*.tmp"))

    def test_append_rejects_truncated_existing_jsonl(self, tmp_path):
        path = tmp_path / "negcache.jsonl"
        path.write_bytes(b'{"partial": true}')
        cache = NegativeCache()

        with pytest.raises(ValueError, match="truncated final line"):
            cache.append_to(path, _entry())

        assert len(cache) == 0
        assert path.read_bytes() == b'{"partial": true}'


def true_line():
    return "not-a-valid-entry"


class TestDumpSnapshotSemantics:
    def test_dump_overwrites_instead_of_duplicating(self, tmp_path):
        path = tmp_path / "dump.jsonl"
        cache = NegativeCache([_entry()])
        cache.dump(path)
        cache.dump(path)
        loaded = NegativeCache.load(path)
        assert len(loaded) == 1
        assert loaded.entries[0].digest == _entry().digest

    def test_indexed_lookup_matches_scan_semantics(self):
        entry = _entry()
        cache = NegativeCache([entry, _entry(metric_id="other")])
        assert len(cache.lookup_key(entry.identity.key)) == 2
        assert cache.lookup_key("sha256:" + "0" * 64) == []

    def test_failed_atomic_replace_preserves_previous_snapshot(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "dump.jsonl"
        original = _entry()
        NegativeCache([original]).dump(path)

        def fail_replace(source, destination):
            raise OSError(f"injected replace failure for {destination}")

        monkeypatch.setattr(cache_module.os, "replace", fail_replace)
        with pytest.raises(OSError, match="injected replace failure"):
            NegativeCache([_entry(metric_id="replacement")]).dump(path)

        loaded = NegativeCache.load(path)
        assert [entry.digest for entry in loaded.entries] == [original.digest]
        assert not list(tmp_path.glob(".dump.jsonl.*.tmp"))
