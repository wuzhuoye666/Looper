from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from looper_core.system_opt.executor import ConfigSnapshot, OperationStatus, SnapshotEntry
from looper_core.system_opt.rollback import (
    LEGACY_ROLLBACK_SCHEMA,
    REGRESSION_DEPENDENCY,
    LegacyRollbackRecord,
    PhaseRestoration,
    RestorationStatus,
    RollbackLevel,
    RollbackRecord,
    RollbackStatus,
    load_rollback_record,
    verify_phase_restoration,
)

FIXED_AT = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
EVIDENCE = ["sha256:" + "b" * 64]


def _entry(item_id: str, value: str, status: OperationStatus = OperationStatus.SUCCEEDED):
    return SnapshotEntry(item_id=item_id, target=f"/sys/{item_id}", status=status, value=value)


def _snapshot(entries: dict[str, SnapshotEntry], target: str = "t1") -> ConfigSnapshot:
    return ConfigSnapshot(target_id=target, entries=entries)


def _record(**overrides):
    payload = dict(
        level=RollbackLevel.CANDIDATE,
        target_id="t1",
        item_ids=["cpufreq-governor-uniform"],
        trigger="measurement finished; candidate not accepted",
        status=RollbackStatus.COMPLETED,
        verified=True,
        evidence_digests=EVIDENCE,
        recorded_at=FIXED_AT,
    )
    payload.update(overrides)
    return RollbackRecord(**payload)


class TestRollbackRecordContract:
    def test_valid_candidate_record_round_trips(self):
        record = _record()
        assert RollbackRecord.model_validate_json(record.model_dump_json()) == record

    def test_candidate_level_requires_item_ids(self):
        with pytest.raises(ValueError, match="reverted item ids"):
            _record(item_ids=[])

    def test_completed_requires_verified_readback(self):
        with pytest.raises(ValueError, match="verified readback"):
            _record(verified=False)

    def test_failed_status_allows_unverified(self):
        record = _record(status=RollbackStatus.FAILED, verified=False)
        assert record.status is RollbackStatus.FAILED

    def test_regression_level_requires_executable_s8_bindings(self):
        with pytest.raises(ValueError, match="checkpoint, result-vector, and threshold"):
            _record(level=RollbackLevel.REGRESSION)
        record = _record(
            level=RollbackLevel.REGRESSION,
            checkpoint_digest="sha256:" + "c" * 64,
            regression_vector_digest="sha256:" + "d" * 64,
            regression_threshold=0.25,
            baseline_snapshot_digest="sha256:" + "e" * 64,
            final_snapshot_digest="sha256:" + "e" * 64,
        )
        assert record.level is RollbackLevel.REGRESSION

    def test_v1alpha1_regression_placeholder_still_loads_without_rewrite(self):
        payload = _record().model_dump(mode="json")
        payload.update(
            {
                "schema_version": LEGACY_ROLLBACK_SCHEMA,
                "level": RollbackLevel.REGRESSION,
                "note": REGRESSION_DEPENDENCY,
            }
        )
        for field in (
            "checkpoint_digest",
            "regression_vector_digest",
            "regression_threshold",
        ):
            payload.pop(field, None)

        loaded = load_rollback_record(json.dumps(payload))

        assert isinstance(loaded, LegacyRollbackRecord)
        assert loaded.schema_version == LEGACY_ROLLBACK_SCHEMA
        assert loaded.note == REGRESSION_DEPENDENCY

    def test_current_schema_dispatches_to_current_model(self):
        record = _record()

        loaded = load_rollback_record(record.model_dump_json())

        assert isinstance(loaded, RollbackRecord)
        assert loaded == record

    def test_evidence_digests_must_be_present_and_unique(self):
        with pytest.raises(ValueError):
            _record(evidence_digests=[])
        with pytest.raises(ValueError, match="unique"):
            _record(evidence_digests=EVIDENCE + EVIDENCE)


class TestPhaseRestoration:
    def test_identical_complete_snapshots_are_restored(self):
        baseline = _snapshot({"a": _entry("a", "schedutil")})
        actual = _snapshot({"a": _entry("a", "schedutil")})
        result = verify_phase_restoration(actual, baseline)
        assert result.status is RestorationStatus.RESTORED
        assert result.actual_snapshot_digest == result.baseline_snapshot_digest

    def test_value_difference_is_mismatch(self):
        baseline = _snapshot({"a": _entry("a", "schedutil")})
        actual = _snapshot({"a": _entry("a", "performance")})
        result = verify_phase_restoration(actual, baseline)
        assert result.status is RestorationStatus.MISMATCH
        assert result.differing_items == ["a"]

    def test_failed_entry_is_incomplete_even_if_digest_would_match(self):
        baseline = _snapshot({"a": _entry("a", "schedutil")})
        actual = _snapshot({"a": _entry("a", "schedutil", status=OperationStatus.FAILED)})
        result = verify_phase_restoration(actual, baseline)
        assert result.status is RestorationStatus.INCOMPLETE
        assert result.incomplete_items == ["a"]

    def test_missing_item_is_incomplete(self):
        baseline = _snapshot({"a": _entry("a", "x"), "b": _entry("b", "y")})
        actual = _snapshot({"a": _entry("a", "x")})
        result = verify_phase_restoration(actual, baseline)
        assert result.status is RestorationStatus.INCOMPLETE
        assert result.missing_items == ["b"]

    def test_extra_item_is_mismatch_not_restored(self):
        baseline = _snapshot({"a": _entry("a", "x")})
        actual = _snapshot({"a": _entry("a", "x"), "b": _entry("b", "y")})
        result = verify_phase_restoration(actual, baseline)
        assert result.status is RestorationStatus.MISMATCH
        assert result.extra_items == ["b"]

    def test_target_mismatch_fails_closed(self):
        with pytest.raises(ValueError, match="different targets"):
            verify_phase_restoration(_snapshot({"a": _entry("a", "x")}, "t1"),
                                     _snapshot({"a": _entry("a", "x")}, "t2"))

    def test_phase_restoration_digest_is_stable(self):
        baseline = _snapshot({"a": _entry("a", "x")})
        actual = _snapshot({"a": _entry("a", "x")})
        first = verify_phase_restoration(actual, baseline)
        second = verify_phase_restoration(actual, baseline)
        assert first.digest == second.digest
        assert PhaseRestoration.model_validate_json(first.model_dump_json()).digest == first.digest
