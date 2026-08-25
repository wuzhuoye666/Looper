"""S4 target-local scale/reference approval evidence.

This module deliberately does not derive a scale.  It binds a task-approved
``MetricContract`` to the target, workload, pressure protocol, formula version,
and the calibration batches that informed the approval.  A policy can enter
online routing only when every component-diagnostic metric matches one approved
entry exactly.  Missing or unavailable entries fail closed.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from looper_core.canonical import canonical_digest, canonical_json
from looper_core.contracts import StrictModel
from looper_core.system_opt.policy import (
    MetricContract,
    MetricRole,
    OptimizationMode,
    SystemOptimizationPolicy,
)

S4_SCALE_CALIBRATION_SCHEMA = "looper.s4-scale-calibration/v1alpha1"
S4_SCALE_CALIBRATION_INDEX_SCHEMA = "looper.s4-scale-calibration-index/v1alpha1"
S4_FORMULA_ID = "F-PROJECT-S4-PIECEWISE-LINEAR/v1alpha1"
_DIGEST = r"^sha256:[0-9a-f]{64}$"
_BUNDLE_FILENAME = re.compile(r"^s4-scale-calibration-([0-9a-f]{64})\.json$")
_INDEX_FILENAME = "s4-scale-calibration-index.json"


class ApprovedMetricCalibration(StrictModel):
    status: Literal["approved"]
    metric_contract: MetricContract
    calibration_batch_digests: list[str] = Field(min_length=1)
    calibration_basis: str = Field(min_length=1, max_length=2000)
    approval_evidence_digest: str = Field(pattern=_DIGEST)
    approved_by: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_approved_metric(self) -> ApprovedMetricCalibration:
        if self.metric_contract.role is not MetricRole.COMPONENT_DIAGNOSTIC:
            raise ValueError("S4 calibration can approve only component-diagnostic metrics")
        if self.calibration_batch_digests != sorted(
            set(self.calibration_batch_digests)
        ):
            raise ValueError("calibration batch digests must be unique and sorted")
        if any(
            not _strict_digest(digest) for digest in self.calibration_batch_digests
        ):
            raise ValueError("calibration batch digests must be strict lowercase sha256")
        return self

    @property
    def metric_id(self) -> str:
        return self.metric_contract.id

    @property
    def component(self) -> str:
        return self.metric_contract.component


class UnavailableMetricCalibration(StrictModel):
    status: Literal["unavailable"]
    metric_id: str = Field(min_length=1, max_length=160)
    component: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=2000)
    capability_evidence_digests: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unavailable_metric(self) -> UnavailableMetricCalibration:
        if self.capability_evidence_digests != sorted(
            set(self.capability_evidence_digests)
        ):
            raise ValueError("capability evidence digests must be unique and sorted")
        if any(
            not _strict_digest(digest)
            for digest in self.capability_evidence_digests
        ):
            raise ValueError("capability evidence digests must be strict lowercase sha256")
        return self


S4MetricCalibration = Annotated[
    ApprovedMetricCalibration | UnavailableMetricCalibration,
    Field(discriminator="status"),
]


class S4ScaleCalibrationBundle(StrictModel):
    schema_version: Literal[S4_SCALE_CALIBRATION_SCHEMA] = S4_SCALE_CALIBRATION_SCHEMA
    target_id: str = Field(min_length=1, max_length=160)
    environment_digest: str = Field(pattern=_DIGEST)
    workload_contract_digest: str = Field(pattern=_DIGEST)
    pressure_protocol_digest: str = Field(pattern=_DIGEST)
    formula_id: Literal[S4_FORMULA_ID]
    entries: list[S4MetricCalibration] = Field(min_length=1)
    counting_basis: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_entry_order(self) -> S4ScaleCalibrationBundle:
        metric_ids = [entry.metric_id for entry in self.entries]
        if metric_ids != sorted(set(metric_ids)):
            raise ValueError("S4 calibration entries must be unique and sorted by metric_id")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class S4ScaleCalibrationIndex(StrictModel):
    schema_version: Literal[S4_SCALE_CALIBRATION_INDEX_SCHEMA] = (
        S4_SCALE_CALIBRATION_INDEX_SCHEMA
    )
    bundle_digest: str = Field(pattern=_DIGEST)
    bundle_filename: str = Field(
        pattern=r"^s4-scale-calibration-[0-9a-f]{64}\.json$"
    )


def _strict_digest(value: str) -> bool:
    return (
        len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def verify_s4_scale_calibration(
    policy: SystemOptimizationPolicy,
    bundle: S4ScaleCalibrationBundle,
    *,
    target_id: str,
    environment_digest: str,
    workload_contract_digest: str,
    pressure_protocol_digest: str,
) -> list[MetricContract]:
    """Return approved diagnostics only after every identity and contract matches."""

    expected_identity = {
        "target_id": target_id,
        "environment_digest": environment_digest,
        "workload_contract_digest": workload_contract_digest,
        "pressure_protocol_digest": pressure_protocol_digest,
    }
    actual_identity = {
        "target_id": bundle.target_id,
        "environment_digest": bundle.environment_digest,
        "workload_contract_digest": bundle.workload_contract_digest,
        "pressure_protocol_digest": bundle.pressure_protocol_digest,
    }
    if actual_identity != expected_identity:
        differing = sorted(
            key for key in expected_identity if actual_identity[key] != expected_identity[key]
        )
        raise ValueError(f"S4 calibration identity differs on {differing}")
    if policy.mode is not OptimizationMode.WORKLOAD:
        raise ValueError("S4 online calibration requires a workload policy")

    diagnostics = {
        metric.id: metric
        for metric in policy.metrics
        if metric.role is MetricRole.COMPONENT_DIAGNOSTIC
    }
    entries = {entry.metric_id: entry for entry in bundle.entries}
    if set(entries) != set(diagnostics):
        missing = sorted(set(diagnostics) - set(entries))
        extra = sorted(set(entries) - set(diagnostics))
        raise ValueError(
            f"S4 calibration coverage mismatch: missing={missing}, extra={extra}"
        )

    approved: list[MetricContract] = []
    for metric_id in sorted(diagnostics):
        entry = entries[metric_id]
        if isinstance(entry, UnavailableMetricCalibration):
            raise ValueError(
                f"S4 diagnostic metric {metric_id!r} is unavailable: {entry.reason}"
            )
        policy_metric = diagnostics[metric_id]
        if canonical_json(entry.metric_contract.model_dump(mode="json")) != canonical_json(
            policy_metric.model_dump(mode="json")
        ):
            raise ValueError(
                f"S4 calibration approves a different MetricContract for {metric_id!r}"
            )
        approved.append(entry.metric_contract)
    return approved


def _atomic_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".s4-calibration.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    descriptor_open = True
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor_open = False
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor_open:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def persist_s4_scale_calibration(
    control_dir: Path, bundle: S4ScaleCalibrationBundle
) -> S4ScaleCalibrationIndex:
    index_path = control_dir / _INDEX_FILENAME
    existing_files = [
        path
        for path in control_dir.glob("s4-scale-calibration-*.json")
        if path.name != _INDEX_FILENAME
    ]
    if index_path.exists():
        existing = load_s4_scale_calibration(control_dir)
        if existing.digest != bundle.digest:
            raise ValueError(
                "S4 calibration directory already publishes a different bundle"
            )
    elif existing_files:
        raise ValueError("S4 calibration evidence exists without its fixed index")

    filename = f"s4-scale-calibration-{bundle.digest.removeprefix('sha256:')}.json"
    index = S4ScaleCalibrationIndex(
        bundle_digest=bundle.digest,
        bundle_filename=filename,
    )
    _atomic_write(control_dir / filename, bundle.model_dump(mode="json"))
    _atomic_write(
        index_path,
        index.model_dump(mode="json"),
    )
    return index


def load_s4_scale_calibration(control_dir: Path) -> S4ScaleCalibrationBundle:
    """Load one indexed bundle after content-address and orphan checks.

    This proves only internal file integrity.  Approval authenticity remains
    bound to the externally reviewed ``approval_evidence_digest`` and operator
    identity; this self-contained index is not a signature or trust anchor.
    """

    index_path = control_dir / _INDEX_FILENAME
    if not index_path.is_file():
        raise ValueError("S4 calibration index is missing")
    index = S4ScaleCalibrationIndex.model_validate_json(
        index_path.read_text(encoding="utf-8")
    )

    evidence_files = [
        path
        for path in control_dir.glob("s4-scale-calibration-*.json")
        if path.name != _INDEX_FILENAME
    ]
    malformed = sorted(
        path.name for path in evidence_files if _BUNDLE_FILENAME.fullmatch(path.name) is None
    )
    if malformed:
        raise ValueError(f"malformed S4 calibration evidence filenames: {malformed}")
    names = {path.name for path in evidence_files}
    expected = {index.bundle_filename}
    if names != expected:
        missing = sorted(expected - names)
        orphan = sorted(names - expected)
        raise ValueError(
            f"S4 calibration file set mismatch: missing={missing}, orphan={orphan}"
        )

    bundle = S4ScaleCalibrationBundle.model_validate_json(
        (control_dir / index.bundle_filename).read_text(encoding="utf-8")
    )
    if bundle.digest != index.bundle_digest:
        raise ValueError("S4 calibration bundle digest does not match the index")
    digest_hex = index.bundle_digest.removeprefix("sha256:")
    if index.bundle_filename != f"s4-scale-calibration-{digest_hex}.json":
        raise ValueError("S4 calibration filename does not match its digest")
    return bundle


__all__ = [
    "S4_FORMULA_ID",
    "S4_SCALE_CALIBRATION_INDEX_SCHEMA",
    "S4_SCALE_CALIBRATION_SCHEMA",
    "ApprovedMetricCalibration",
    "S4MetricCalibration",
    "S4ScaleCalibrationBundle",
    "S4ScaleCalibrationIndex",
    "UnavailableMetricCalibration",
    "load_s4_scale_calibration",
    "persist_s4_scale_calibration",
    "verify_s4_scale_calibration",
]
