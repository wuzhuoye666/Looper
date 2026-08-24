from pathlib import Path

import pytest
from looper_core.system_opt.demo import build_demo_policy
from looper_core.system_opt.policy import MetricRole, OptimizationMode
from looper_core.system_opt.priority_calibration import (
    S4_FORMULA_ID,
    ApprovedMetricCalibration,
    S4ScaleCalibrationBundle,
    UnavailableMetricCalibration,
    load_s4_scale_calibration,
    persist_s4_scale_calibration,
    verify_s4_scale_calibration,
)

DIGESTS = [f"sha256:{index:064x}" for index in range(1, 20)]


def _bundle(*, unavailable_metric: str | None = None) -> S4ScaleCalibrationBundle:
    policy = build_demo_policy(OptimizationMode.WORKLOAD)
    diagnostics = sorted(
        (metric for metric in policy.metrics if metric.role is MetricRole.COMPONENT_DIAGNOSTIC),
        key=lambda metric: metric.id,
    )
    entries = []
    for index, metric in enumerate(diagnostics):
        if metric.id == unavailable_metric:
            entries.append(
                UnavailableMetricCalibration(
                    status="unavailable",
                    metric_id=metric.id,
                    component=metric.component,
                    reason="target capability probe could not obtain this metric",
                    capability_evidence_digests=[DIGESTS[index]],
                )
            )
        else:
            entries.append(
                ApprovedMetricCalibration(
                    status="approved",
                    metric_contract=metric,
                    calibration_batch_digests=[DIGESTS[index]],
                    calibration_basis="explicit synthetic test approval; not an empirical default",
                    approval_evidence_digest=DIGESTS[index + 10],
                    approved_by="test-task-owner",
                )
            )
    return S4ScaleCalibrationBundle(
        target_id="target-1",
        environment_digest=DIGESTS[15],
        workload_contract_digest=DIGESTS[16],
        pressure_protocol_digest=DIGESTS[17],
        formula_id=S4_FORMULA_ID,
        entries=entries,
        counting_basis="one explicit entry per diagnostic metric in the bound policy",
    )


def _verify(bundle: S4ScaleCalibrationBundle):
    return verify_s4_scale_calibration(
        build_demo_policy(OptimizationMode.WORKLOAD),
        bundle,
        target_id="target-1",
        environment_digest=DIGESTS[15],
        workload_contract_digest=DIGESTS[16],
        pressure_protocol_digest=DIGESTS[17],
    )


def test_complete_explicit_bundle_approves_every_diagnostic_and_persists(
    tmp_path: Path,
) -> None:
    bundle = _bundle()

    approved = _verify(bundle)
    index = persist_s4_scale_calibration(tmp_path, bundle)

    assert [metric.id for metric in approved] == sorted(
        entry.metric_id for entry in bundle.entries
    )
    assert (tmp_path / index.bundle_filename).is_file()
    assert (tmp_path / "s4-scale-calibration-index.json").is_file()
    assert load_s4_scale_calibration(tmp_path) == bundle


def test_policy_scale_change_invalidates_prior_approval() -> None:
    bundle = _bundle()
    policy = build_demo_policy(OptimizationMode.WORKLOAD)
    metric = next(
        item for item in policy.metrics if item.role is MetricRole.COMPONENT_DIAGNOSTIC
    )
    changed = metric.model_copy(update={"scale": metric.scale * 2 if metric.scale else 2.0})
    policy = policy.model_copy(
        update={
            "metrics": [changed if item.id == metric.id else item for item in policy.metrics]
        }
    )

    with pytest.raises(ValueError, match="different MetricContract"):
        verify_s4_scale_calibration(
            policy,
            bundle,
            target_id="target-1",
            environment_digest=DIGESTS[15],
            workload_contract_digest=DIGESTS[16],
            pressure_protocol_digest=DIGESTS[17],
        )


def test_missing_extra_or_unavailable_metric_fails_closed() -> None:
    complete = _bundle()
    missing = complete.model_copy(update={"entries": complete.entries[1:]})
    with pytest.raises(ValueError, match="coverage mismatch"):
        _verify(missing)

    unavailable_id = complete.entries[0].metric_id
    with pytest.raises(ValueError, match="is unavailable"):
        _verify(_bundle(unavailable_metric=unavailable_id))


def test_bundle_rejects_duplicate_unsorted_or_malformed_evidence() -> None:
    complete = _bundle()
    with pytest.raises(ValueError, match="unique and sorted"):
        S4ScaleCalibrationBundle.model_validate(
            {**complete.model_dump(mode="json"), "entries": list(reversed(complete.entries))}
        )
    approved = next(
        entry for entry in complete.entries if isinstance(entry, ApprovedMetricCalibration)
    )
    with pytest.raises(ValueError, match="strict lowercase"):
        ApprovedMetricCalibration.model_validate(
            {
                **approved.model_dump(mode="json"),
                "calibration_batch_digests": ["sha256:" + "A" * 64],
            }
        )


def test_persisted_bundle_rejects_tampering_or_orphan(tmp_path) -> None:
    bundle = _bundle()
    index = persist_s4_scale_calibration(tmp_path, bundle)
    bundle_path = tmp_path / index.bundle_filename
    bundle_path.write_text(
        bundle_path.read_text(encoding="utf-8").replace(
            '"target_id": "target-1"', '"target_id": "forged-target"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="digest does not match"):
        load_s4_scale_calibration(tmp_path)

    orphan_dir = tmp_path / "orphan-case"
    persist_s4_scale_calibration(orphan_dir, bundle)
    (orphan_dir / f"s4-scale-calibration-{'f' * 64}.json").write_text(
        "{}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="orphan"):
        load_s4_scale_calibration(orphan_dir)


def test_persisted_bundle_rejects_missing_index_or_malformed_filename(
    tmp_path,
) -> None:
    with pytest.raises(ValueError, match="index is missing"):
        load_s4_scale_calibration(tmp_path)

    persist_s4_scale_calibration(tmp_path, _bundle())
    (tmp_path / "s4-scale-calibration-not-a-digest.json").write_text(
        "{}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="malformed"):
        load_s4_scale_calibration(tmp_path)


def test_persist_refuses_to_replace_a_different_published_bundle(tmp_path) -> None:
    original = _bundle()
    persist_s4_scale_calibration(tmp_path, original)
    changed = original.model_copy(update={"target_id": "target-2"})

    with pytest.raises(ValueError, match="different bundle"):
        persist_s4_scale_calibration(tmp_path, changed)

    assert load_s4_scale_calibration(tmp_path) == original
