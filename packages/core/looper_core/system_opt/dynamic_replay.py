"""D4-R4 动态证据回放验证器：重算 digest + 校验内部完整性与关联一致性。

架构层：L4 证据链对侧（docs/system-optimizer/contracts/dynamic-session-files.md）。
与 ``dynamic_collection.persist_dynamic_collection_evidence`` 相对——本模块**只回放与
验证**，不设阈值、不做裁决。

验证范围（诚实声明）：本验证器核对的是**内部完整性**与**关联一致性**——每个
digest 地址文件的内容能否重算出索引声明的 digest、O1 run 与 O1 overhead 是否
逐组件绑定、O2 probe 与 O2 overhead 是否精确一对一、窗口/组件/目标/环境身份是否
一致、有无重复/畸形/孤儿文件。**它不提供真实性**：索引与文件同被攻击者掌控时，
攻击者可同时重算 digest 使其自洽；真实性需要外部 manifest、签名或可信锚点。

路径安全：所有 digest 在索引模型加载阶段即按 ``^sha256:[0-9a-f]{64}$`` 严格校验，
``collection_evidence_filename`` 再做一次完整匹配与 kind allowlist 校验；任何非法
digest/kind 都在读取证据文件之前以 :class:`DynamicEvidenceVerificationError` 拒绝，
不可能用 ``..`` 或分隔符逃出 ``control_dir``。

O1 overhead 语义（集合级，2026-08-24 用户确认）：同一窗口内多个组件**允许共享同一个
overhead digest**；本验证器对 O1 只验证集合成员关系与逐组件 target/environment/collector
一致、overhead 的 measurement identity 唯一回指首个产出窗口，不宣称单组件归因。
O2 probe↔overhead 语义（精确一对一）：每个 O2 probe 必须恰好引用一个、且仅一个
O2 overhead digest，重复引用一律 fail-closed。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.collector import (
    CollectionOverheadABEvidence,
    ComponentCollectionRun,
)
from looper_core.system_opt.dynamic_collection import (
    DynamicCollectionEvidenceIndex,
    O2ComponentProbeEvidence,
    collection_evidence_filename,
)

VERIFICATION_SCHEMA = "looper.dynamic-collection-evidence-verification/v1alpha1"
INDEX_FILENAME = "dynamic-collection-evidence-index.json"
_DIGEST = r"^sha256:[0-9a-f]{64}$"

_KIND_MODELS = {
    "o1-collection-run": ComponentCollectionRun,
    "o1-overhead-evidence": CollectionOverheadABEvidence,
    "o2-probe-evidence": O2ComponentProbeEvidence,
    "o2-overhead-evidence": CollectionOverheadABEvidence,
}
_EVIDENCE_KINDS = tuple(_KIND_MODELS)
_EVIDENCE_FILENAME = re.compile(
    r"^(o1-collection-run|o1-overhead-evidence|o2-probe-evidence|o2-overhead-evidence)"
    r"-([0-9a-f]{64})\.json$"
)


class DynamicEvidenceVerificationError(ValueError):
    """The persisted live collection evidence failed replay verification."""


class DynamicEvidenceVerification(StrictModel):
    """Replayed + verified evidence；不派生任何阈值或裁决。"""

    schema_version: Literal[VERIFICATION_SCHEMA] = VERIFICATION_SCHEMA
    index: DynamicCollectionEvidenceIndex
    o1_runs_by_window: dict[str, list[ComponentCollectionRun]]
    o1_overhead_by_digest: dict[str, CollectionOverheadABEvidence]
    o2_probe_by_digest: dict[str, O2ComponentProbeEvidence]
    o2_overhead_by_digest: dict[str, CollectionOverheadABEvidence]


def _load_and_verify(control_dir: Path, kind: str, digest: str):
    """Read one digest-addressed file, re-validate its model, and re-check its digest."""

    model_cls = _KIND_MODELS[kind]
    try:
        filename = collection_evidence_filename(kind, digest)
    except ValueError as error:
        raise DynamicEvidenceVerificationError(str(error)) from error
    path = control_dir / filename
    if not path.is_file():
        raise DynamicEvidenceVerificationError(f"missing evidence file: {filename}")
    try:
        model = model_cls.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise DynamicEvidenceVerificationError(
            f"invalid {kind} file {filename}: {error}"
        ) from error
    if model.digest != digest:
        raise DynamicEvidenceVerificationError(
            f"digest mismatch for {filename}: index declares {digest}, "
            f"content recomputes to {model.digest}"
        )
    return model


def _no_duplicates(values, label: str) -> None:
    if len(values) != len(set(values)):
        raise DynamicEvidenceVerificationError(f"duplicate {label} digest in index")


def _require_mid(identity: dict[str, str], key: str, label: str) -> str:
    value = identity.get(key)
    if value is None:
        raise DynamicEvidenceVerificationError(f"{label} measurement identity lacks {key!r}")
    return value


def verify_dynamic_collection_evidence(
    control_dir: Path,
) -> DynamicEvidenceVerification:
    """Replay and verify the persisted live O1/O2 evidence under ``control/``.

    Fail-closed on：缺索引/缺证据文件、非法 digest（路径穿越/非 hex/大写/非法 kind）、
    重复 digest、forged window、run↔overhead 逐组件身份不一致、O1 overhead 无法唯一
    回指产出窗、O2 probe↔overhead 非精确一对一（含重复引用）、畸形/孤儿证据文件、
    digest 篡改、悬空引用。
    """

    index_path = control_dir / INDEX_FILENAME
    if not index_path.is_file():
        raise DynamicEvidenceVerificationError(f"missing index file: {index_path}")
    try:
        index = DynamicCollectionEvidenceIndex.model_validate_json(
            index_path.read_text(encoding="utf-8")
        )
    except Exception as error:
        raise DynamicEvidenceVerificationError(f"invalid index file: {error}") from error

    # 重复 digest 拒绝：O1 run 跨窗、O2 probe、O2 overhead 均不得重复。
    o1_run_digests = [d for digests in index.o1_runs_by_window.values() for d in digests]
    _no_duplicates(o1_run_digests, "O1 run")
    _no_duplicates(index.o2_probe_evidence_digests, "O2 probe")
    _no_duplicates(index.o2_overhead_evidence_digests, "O2 overhead")
    o1_overhead_digests = {
        d for bindings in index.o1_overhead_digests_by_window.values() for d in bindings.values()
    }

    # 逐文件重算 digest。
    o1_runs_by_digest: dict[str, ComponentCollectionRun] = {}
    o1_overhead_by_digest: dict[str, CollectionOverheadABEvidence] = {}
    o2_probe_by_digest: dict[str, O2ComponentProbeEvidence] = {}
    o2_overhead_by_digest: dict[str, CollectionOverheadABEvidence] = {}
    for digest in o1_run_digests:
        o1_runs_by_digest[digest] = _load_and_verify(control_dir, "o1-collection-run", digest)
    for digest in o1_overhead_digests:
        o1_overhead_by_digest[digest] = _load_and_verify(
            control_dir, "o1-overhead-evidence", digest
        )
    for digest in index.o2_probe_evidence_digests:
        o2_probe_by_digest[digest] = _load_and_verify(control_dir, "o2-probe-evidence", digest)
    for digest in index.o2_overhead_evidence_digests:
        o2_overhead_by_digest[digest] = _load_and_verify(
            control_dir, "o2-overhead-evidence", digest
        )

    # 窗口一致性：O1 run 与 O1 overhead 绑定的窗口集合必须完全一致。
    if set(index.o1_runs_by_window) != set(index.o1_overhead_digests_by_window):
        raise DynamicEvidenceVerificationError(
            "O1 run windows do not exactly match overhead-bound windows"
        )

    # O1 run 有效性：enabled、window_id 等于索引窗口、层级 O1。
    for window_id, digests in index.o1_runs_by_window.items():
        for digest in digests:
            run = o1_runs_by_digest[digest]
            if not run.enabled:
                raise DynamicEvidenceVerificationError(
                    f"O1 run {digest} is not an enabled collection"
                )
            mid = run.request.measurement_identity
            if _require_mid(mid, "window_id", "O1 run") != window_id:
                raise DynamicEvidenceVerificationError(
                    f"O1 run {digest} window_id does not match its index window {window_id!r}"
                )
            if _require_mid(mid, "observation_layer", "O1 run") != "O1":
                raise DynamicEvidenceVerificationError(
                    f"O1 run {digest} observation layer is not O1"
                )

    # 每窗组件集合一致性：run 组件无重复，且与 overhead 绑定组件完全一致。
    # （允许同窗多组件共享同一 overhead digest，故不比对各组件 overhead digest 的互异性。）
    for window_id, bindings in index.o1_overhead_digests_by_window.items():
        runs = [o1_runs_by_digest[d] for d in index.o1_runs_by_window[window_id]]
        run_components = [run.request.component for run in runs]
        if len(run_components) != len(set(run_components)):
            raise DynamicEvidenceVerificationError(
                f"window {window_id!r} repeats a component run"
            )
        if set(run_components) != set(bindings):
            raise DynamicEvidenceVerificationError(
                f"window {window_id!r} components are not exactly bound to overhead evidence"
            )

    # 逐组件 run↔overhead 关联（集合级）：target / environment / collector 一致。
    for window_id, bindings in index.o1_overhead_digests_by_window.items():
        runs_by_component = {
            run.request.component: run
            for run in (o1_runs_by_digest[d] for d in index.o1_runs_by_window[window_id])
        }
        for component, overhead_digest in bindings.items():
            run = runs_by_component[component]
            overhead = o1_overhead_by_digest[overhead_digest]
            if overhead.collector_id != run.request.collector_id:
                raise DynamicEvidenceVerificationError(
                    f"window {window_id!r} component {component}: overhead collector_id "
                    f"{overhead.collector_id!r} does not match run collector_id "
                    f"{run.request.collector_id!r}"
                )
            if overhead.target_id != run.request.target_id:
                raise DynamicEvidenceVerificationError(
                    f"window {window_id!r} component {component}: "
                    "overhead target_id does not match run"
                )
            if overhead.environment_digest != run.request.environment_digest:
                raise DynamicEvidenceVerificationError(
                    f"window {window_id!r} component {component}: overhead environment_digest "
                    "does not match run"
                )

    # O1 overhead 的 measurement identity 唯一回指其首个产出窗口（不要求唯一回指组件）。
    window_mid: dict[str, str] = {}
    for window_id, digests in index.o1_runs_by_window.items():
        mids = {
            canonical_digest(o1_runs_by_digest[d].request.measurement_identity) for d in digests
        }
        if len(mids) != 1:
            raise DynamicEvidenceVerificationError(
                f"O1 window {window_id!r} runs disagree on measurement identity"
            )
        window_mid[window_id] = next(iter(mids))
    for digest, overhead in o1_overhead_by_digest.items():
        mid = overhead.workload_identity.get("measurement_identity_digest")
        producing = [w for w, m in window_mid.items() if m == mid]
        if len(producing) != 1:
            raise DynamicEvidenceVerificationError(
                f"O1 overhead {digest} measurement identity does not uniquely resolve "
                f"to one window (resolved to {len(producing)})"
            )

    # O2 probe ↔ O2 overhead 精确一对一：每个 probe 恰好引用一个 overhead，
    # 每个 overhead 恰好被一个 probe 引用；重复引用立即 fail-closed。
    seen_overhead: dict[str, str] = {}
    for digest in index.o2_probe_evidence_digests:
        probe = o2_probe_by_digest[digest]
        run = probe.collection_run
        overhead_digest = probe.collection_overhead_evidence_digest
        if overhead_digest is None:
            raise DynamicEvidenceVerificationError(
                f"O2 probe {digest} is missing its overhead binding"
            )
        if overhead_digest not in o2_overhead_by_digest:
            raise DynamicEvidenceVerificationError(
                f"O2 probe {digest} references a dangling overhead digest {overhead_digest}"
            )
        if overhead_digest in seen_overhead:
            raise DynamicEvidenceVerificationError(
                f"O2 probe {digest} shares overhead digest {overhead_digest} with probe "
                f"{seen_overhead[overhead_digest]}: O2 probe->overhead must be one-to-one"
            )
        seen_overhead[overhead_digest] = digest
        overhead = o2_overhead_by_digest[overhead_digest]
        if not run.enabled:
            raise DynamicEvidenceVerificationError(f"O2 probe {digest} run is not enabled")
        if run.request.component != probe.hypothesis.component.value:
            raise DynamicEvidenceVerificationError(
                f"O2 probe {digest} collection component does not match its hypothesis"
            )
        mid = run.request.measurement_identity
        if _require_mid(mid, "observation_layer", "O2 probe") != "O2":
            raise DynamicEvidenceVerificationError(
                f"O2 probe {digest} observation layer is not O2"
            )
        if _require_mid(mid, "hypothesis_digest", "O2 probe") != probe.hypothesis.digest:
            raise DynamicEvidenceVerificationError(
                f"O2 probe {digest} hypothesis digest does not match its measurement identity"
            )
        if (
            _require_mid(mid, "observation_window_digest", "O2 probe")
            != probe.observation_window_digest
        ):
            raise DynamicEvidenceVerificationError(
                f"O2 probe {digest} observation window digest does not match "
                "its measurement identity"
            )
        if overhead.collector_id != run.request.collector_id:
            raise DynamicEvidenceVerificationError(
                f"O2 probe {digest} overhead collector_id does not match its run"
            )
        if overhead.target_id != run.request.target_id:
            raise DynamicEvidenceVerificationError(
                f"O2 probe {digest} overhead target_id does not match its run"
            )
        if overhead.environment_digest != run.request.environment_digest:
            raise DynamicEvidenceVerificationError(
                f"O2 probe {digest} overhead environment_digest does not match its run"
            )
        if overhead.workload_identity.get("measurement_identity_digest") != canonical_digest(mid):
            raise DynamicEvidenceVerificationError(
                f"O2 probe {digest} overhead measurement identity does not match its run"
            )
    if len(index.o2_probe_evidence_digests) != len(index.o2_overhead_evidence_digests):
        raise DynamicEvidenceVerificationError(
            "O2 probe count does not equal O2 overhead count"
        )
    if set(seen_overhead) != set(index.o2_overhead_evidence_digests):
        raise DynamicEvidenceVerificationError(
            "O2 overhead evidence does not exactly match probe references"
        )

    # 畸形/孤儿证据文件 fail-closed。
    referenced = (
        {("o1-collection-run", d) for d in o1_run_digests}
        | {("o1-overhead-evidence", d) for d in o1_overhead_digests}
        | {("o2-probe-evidence", d) for d in index.o2_probe_evidence_digests}
        | {("o2-overhead-evidence", d) for d in index.o2_overhead_evidence_digests}
    )
    for path in control_dir.glob("*.json"):
        name = path.name
        if not any(name.startswith(f"{kind}-") for kind in _EVIDENCE_KINDS):
            continue
        match = _EVIDENCE_FILENAME.match(name)
        if match is None:
            raise DynamicEvidenceVerificationError(f"malformed evidence file: {name}")
        kind, hexdigest = match.group(1), match.group(2)
        if (kind, f"sha256:{hexdigest}") not in referenced:
            raise DynamicEvidenceVerificationError(
                f"orphan evidence file not referenced by the index: {name}"
            )

    return DynamicEvidenceVerification(
        index=index,
        o1_runs_by_window={
            window_id: [o1_runs_by_digest[d] for d in digests]
            for window_id, digests in index.o1_runs_by_window.items()
        },
        o1_overhead_by_digest=o1_overhead_by_digest,
        o2_probe_by_digest=o2_probe_by_digest,
        o2_overhead_by_digest=o2_overhead_by_digest,
    )


__all__ = [
    "DynamicEvidenceVerification",
    "DynamicEvidenceVerificationError",
    "VERIFICATION_SCHEMA",
    "verify_dynamic_collection_evidence",
]
