from __future__ import annotations

import re
from typing import Any, Literal

import yaml
from looper_core.canonical import canonical_digest, new_id, utc_now
from looper_core.manifest import ManifestError, validate_document
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from looper_api.events import append_event
from looper_api.models import BenchmarkRecord, BenchmarkRegistrationRecord
from looper_api.retired_benchmarks import is_retired_benchmark


def _camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(item.capitalize() for item in tail)


class RegistrationModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


class BenchmarkRegistrationDraft(RegistrationModel):
    name: str = Field(default="", max_length=160)
    benchmark_id: str = Field(default="", max_length=100)
    version: str = Field(default="0.1.0", max_length=64)
    source_url: str = Field(default="", max_length=2000)
    source_revision: str = Field(default="", max_length=80)
    license: str = Field(default="", max_length=80)
    category: str = Field(default="unclassified", max_length=80)
    execution_model: Literal[
        "batch-suite",
        "service-stack",
        "database",
        "storage",
        "network",
        "distributed",
        "accelerator",
        "custom",
    ] = "custom"
    decision_question: str = Field(default="", max_length=1000)
    primary_metric: str = Field(default="", max_length=120)
    primary_unit: str = Field(default="", max_length=40)
    correctness_contract: str = Field(default="", max_length=2000)
    runtime_type: Literal["container", "local-process", "benchexec"] = "container"
    execution_status: Literal["stage0-adapter-only", "executable"] = "stage0-adapter-only"
    image: str = Field(default="", max_length=1000)
    minimum_samples: int = Field(default=1, ge=1, le=10_000_000)
    repeats: int = Field(default=3, ge=1, le=1000)
    has_reference: bool = False
    retains_raw_evidence: bool = True
    cross_environment_audit: bool = True
    manifest: dict[str, Any] | None = None


class BenchmarkRegistrationUpdate(RegistrationModel):
    expected_revision: int = Field(ge=1)
    draft: BenchmarkRegistrationDraft


class BenchmarkRegistrationRegister(RegistrationModel):
    expected_revision: int = Field(ge=1)


class RegistrationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 409,
        code: str = "benchmark_registration_error",
        constraints: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.constraints = constraints


def selection_scenario_document(
    benchmark: BenchmarkRecord,
    registration: BenchmarkRegistrationRecord | None,
) -> dict[str, Any] | None:
    """Return an explicit or registration-derived selection contract.

    Generic Adapter benchmarks historically registered successfully without a
    ``spec.scenario`` section. Their audited registration still contains the
    decision question, primary metric and workload class needed by the
    selection workflow, so expose a conservative single-target contract rather
    than silently hiding them from the experiment form.
    """

    declared = benchmark.manifest_json["spec"].get("scenario")
    if isinstance(declared, dict):
        return declared
    if registration is None or registration.status != "registered":
        return None
    draft = BenchmarkRegistrationDraft.model_validate(registration.draft_json)
    spec = benchmark.manifest_json["spec"]
    adapter = spec.get("adapter") or {}
    primary_metric = draft.primary_metric or adapter.get("primaryMetric")
    if not (
        draft.decision_question
        and primary_metric
        and primary_metric in (spec.get("metrics") or {})
        and spec.get("workloads")
    ):
        return None
    topology = (
        "multi-node"
        if draft.execution_model == "distributed"
        else "client-server"
        if draft.execution_model in {"service-stack", "database", "network"}
        else "single-node"
    )
    workload_class = (
        draft.category if draft.category != "unclassified" else draft.execution_model
    )
    return {
        "id": benchmark.benchmark_id,
        "name": benchmark.name,
        "decision_question": draft.decision_question,
        "user_value": draft.decision_question,
        "workload_class": workload_class,
        "topology": topology,
        "roles": [
            {
                "id": "target",
                "kind": "target",
                "included_in_score": True,
                "description": "Candidate target executing the registered Benchmark Adapter",
            }
        ],
        "primary_metric": primary_metric,
        "slo_gates": [],
    }


def _constraint(
    code: str,
    group: str,
    label: str,
    passed: bool,
    detail: str,
    *,
    blocking: bool = True,
) -> dict[str, Any]:
    return {
        "code": code,
        "group": group,
        "label": label,
        "status": "pass" if passed else "fail",
        "blocking": blocking,
        "detail": detail,
    }


def _is_bootstrap_catalog_record(record: BenchmarkRecord | None) -> bool:
    if record is None or record.package_digest is not None or not record.manifest_path:
        return False
    normalized = record.manifest_path.replace("\\", "/").casefold()
    return (
        "/benchmarks/" in f"/{normalized.strip('/')}"
        and "/benchmark-packages/" not in normalized
    )


def evaluate_registration_constraints(
    draft: BenchmarkRegistrationDraft,
    *,
    session: Session | None = None,
    package_ready: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    constraints: list[dict[str, Any]] = []
    identifier_ok = bool(re.fullmatch(r"[a-z][a-z0-9.-]{2,63}", draft.benchmark_id))
    constraints.append(_constraint(
        "identity.stable-id", "身份", "Benchmark ID 符合 v1alpha1 稳定标识规则",
        identifier_ok, "必须匹配 ^[a-z][a-z0-9.-]{2,63}$。",
    ))
    constraints.append(_constraint(
        "identity.not-retired", "身份", "Benchmark ID 未被永久退役",
        not is_retired_benchmark(draft.benchmark_id),
        "永久退役的 Benchmark ID 不允许重新登记。",
    ))
    revision_ok = bool(
        re.fullmatch(r"[0-9a-f]{40}", draft.source_revision)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", draft.source_revision)
    )
    constraints.append(_constraint(
        "identity.immutable-source", "身份", "来源固定到不可变 commit 或 SHA-256 digest",
        bool(draft.name and draft.version and draft.source_url and draft.license and revision_ok),
        "名称、版本、来源、许可证均必填；revision 必须是完整 commit SHA 或 sha256 digest。",
    ))

    manifest = draft.manifest
    manifest_error: str | None = None
    if manifest is None:
        manifest_error = "尚未提供完整 Benchmark manifest。"
    else:
        try:
            validate_document(manifest, "benchmark-manifest.schema.json")
        except ManifestError as error:
            manifest_error = str(error)
    constraints.append(_constraint(
        "contract.schema", "合同", "完整 manifest 通过 benchmark-manifest JSON Schema",
        manifest_error is None, manifest_error or "schema 与 additionalProperties 约束均通过。",
    ))

    metadata = manifest.get("metadata", {}) if manifest_error is None and manifest else {}
    spec = manifest.get("spec", {}) if manifest_error is None and manifest else {}
    source = metadata.get("source") or {}
    commit = source.get("commit")
    source_digest = source.get("digest")
    declared_revision = commit or source_digest
    identity_matches = bool(
        metadata
        and bool(commit) != bool(source_digest)
        and metadata.get("id") == draft.benchmark_id
        and metadata.get("name") == draft.name
        and metadata.get("version") == draft.version
        and metadata.get("license") == draft.license
        and source.get("url") == draft.source_url
        and declared_revision == draft.source_revision
    )
    constraints.append(_constraint(
        "contract.identity-match", "合同", "页面身份与 manifest 元数据完全一致",
        identity_matches, "禁止页面摘要与不可变 manifest 出现双重事实源。",
    ))

    scenario = spec.get("scenario") or {}
    adapter = spec.get("adapter") or {}
    metrics = spec.get("metrics") or {}
    primary = metrics.get(draft.primary_metric) or {}
    declared_primary_metric = scenario.get("primary_metric") or adapter.get("primaryMetric")
    scenario_matches = bool(
        draft.decision_question.strip()
        and declared_primary_metric == draft.primary_metric
        and (not scenario or scenario.get("decision_question") == draft.decision_question)
        and primary.get("unit") == draft.primary_unit
        and primary.get("direction") in {"minimize", "maximize"}
    )
    constraints.append(_constraint(
        "contract.scenario-semantics", "合同", "采购问题、主指标、单位和方向一致",
        scenario_matches, "主指标必须存在于 metrics，且不能使用 direction=none。",
    ))
    hard_gates = [
        gate for gate in scenario.get("slo_gates", [])
        if gate.get("hard", True) and gate.get("kind") in {"correctness", "safety", "slo"}
    ]
    required_checks = adapter.get("requiredChecks") or []
    constraints.append(_constraint(
        "contract.hard-gates", "合同", "声明不可补偿的正确性、安全或 SLO 门禁",
        bool(hard_gates or required_checks),
        "门禁只从 manifest 的 scenario.slo_gates 或 adapter.requiredChecks 读取，"
        "页面不再维护第二份说明。",
    ))

    infrastructure = spec.get("infrastructure") or {}
    node_groups = infrastructure.get("nodeGroups") or []
    node_group_ids = [item.get("id") for item in node_groups]
    primary_node_group = infrastructure.get("primaryNodeGroup")
    count_ranges_ok = all(
        isinstance(item.get("count"), dict)
        and item["count"].get("minimum", 0)
        <= item["count"].get("default", 0)
        <= item["count"].get("maximum", 0)
        for item in node_groups
    )
    placement_references = [
        reference
        for item in node_groups
        for key in ("coLocateWith", "separateFrom")
        for reference in (item.get("placement") or {}).get(key, [])
    ]
    link_references = [
        reference
        for link in infrastructure.get("links", [])
        for reference in (link.get("source"), link.get("target"))
    ]
    topology_input = next(
        (
            item
            for item in adapter.get("inputs", [])
            if item.get("kind") == "topology" and item.get("required") is True
        ),
        None,
    )
    adapter_managed_multi_node = bool(
        infrastructure.get("orchestration") == "adapter"
        and sum(item.get("count", {}).get("minimum", 0) for item in node_groups) > 1
    )
    infrastructure_consistent = bool(
        not infrastructure
        or (
            len(node_group_ids) == len(set(node_group_ids))
            and all(node_group_ids)
            and primary_node_group in node_group_ids
            and count_ranges_ok
            and all(reference in node_group_ids for reference in placement_references)
            and all(reference in node_group_ids for reference in link_references)
            and (not adapter_managed_multi_node or topology_input is not None)
        )
    )
    topology_requires_machine_contract = bool(
        scenario.get("topology") in {"client-server", "multi-node"}
        or adapter.get("executionModel") in {"distributed", "storage", "network"}
    )
    constraints.append(_constraint(
        "contract.infrastructure-present",
        "基础设施",
        "多机或分布式套件声明每类机器及最低配置",
        not topology_requires_machine_contract or bool(infrastructure),
        "单机套件可以省略；client-server、multi-node、distributed、storage 和 network "
        "套件应声明 spec.infrastructure。",
        blocking=False,
    ))
    constraints.append(_constraint(
        "contract.infrastructure-consistency",
        "基础设施",
        "机器组、数量范围、放置关系和拓扑输入相互一致",
        infrastructure_consistent,
        "primaryNodeGroup 和所有连接/放置引用必须存在；minimum ≤ default ≤ maximum；"
        "Adapter 自编排多机时必须声明 required topology 输入。",
    ))
    looper_managed_multi_node = bool(
        infrastructure.get("orchestration") == "looper"
        and sum(item.get("count", {}).get("minimum", 0) for item in node_groups) > 1
    )
    constraints.append(_constraint(
        "execution.orchestration-support",
        "执行",
        "所选多机编排方式已由当前 Worker 实现",
        not looper_managed_multi_node or draft.execution_status == "stage0-adapter-only",
        "当前可执行多机包必须使用 Adapter 自编排和 required topology 输入；"
        "Looper 多角色调度仍只允许 Stage 0 合同。",
    ))

    runtime = spec.get("runtime") or {}
    extensions = spec.get("x-extensions") or {}
    runtime_matches = bool(
        runtime
        and runtime.get("type") == draft.runtime_type
        and (runtime.get("image") or "") == draft.image
        and extensions.get("executionStatus", "executable") == draft.execution_status
    )
    constraints.append(_constraint(
        "execution.runtime-match", "执行", "运行时和执行成熟度与 manifest 一致",
        runtime_matches, "Stage 0 与 executable 必须显式区分。",
    ))
    image = runtime.get("image") or ""
    pinned_image = bool(re.fullmatch(r"[a-z0-9][a-z0-9._/-]*[a-z0-9]@sha256:[0-9a-f]{64}", image))
    isolation_ok = (
        (
            draft.runtime_type == "container"
            and (draft.execution_status != "executable" or pinned_image)
        )
        or (draft.runtime_type == "benchexec")
        or (draft.runtime_type == "local-process" and spec.get("trust") == "trusted")
    )
    constraints.append(_constraint(
        "execution.isolation", "执行", "执行隔离和镜像固定策略满足要求",
        isolation_ok,
        "executable 容器必须固定 @sha256；untrusted 不得使用 local-process。",
    ))
    local_package_ready = draft.runtime_type != "local-process" or package_ready
    constraints.append(_constraint(
        "execution.package-bundle",
        "执行",
        "本地进程套件包含可自动下发的脚本包",
        draft.execution_status == "stage0-adapter-only" or local_package_ready,
        "可执行 local-process Benchmark 必须以 ZIP 接入包注册，不能只上传 manifest。",
    ))
    commands = runtime.get("commands") or {}
    provisioning = runtime.get("provisioning")
    managed_provisioning = (
        isinstance(provisioning, dict) and provisioning.get("mode") == "managed"
    )
    host_capabilities = (
        set(provisioning.get("hostCapabilities", [])) if managed_provisioning else set()
    )
    provided_capabilities = (
        set(provisioning.get("provides", [])) if managed_provisioning else set()
    )
    benchmark_capabilities = set(spec.get("capabilities", []))
    provisioning_ok = bool(
        not managed_provisioning
        or (
            commands.get("prepare")
            and benchmark_capabilities <= host_capabilities | provided_capabilities
            and provisioning.get("cacheKey") == runtime.get("dependencyLockDigest")
        )
    )
    constraints.append(_constraint(
        "execution.managed-provisioning",
        "执行",
        "自动部署声明覆盖运行依赖并绑定锁文件",
        provisioning_ok,
        "managed provisioning 必须有 prepare、覆盖 spec.capabilities，"
        "且 cacheKey 等于 dependencyLockDigest。",
    ))
    adapter_protocol_ok = bool(
        adapter.get("protocol") == "looper-adapter/v1"
        and adapter.get("executionModel")
        and adapter.get("primaryMetric") == draft.primary_metric
        and adapter.get("requiredChecks")
        and adapter.get("canonicalOutputs")
    )
    adapter_ready = bool(adapter_protocol_ok and commands.get("normalize"))
    constraints.append(_constraint(
        "execution.adapter-protocol",
        "执行",
        "可执行套件使用通用 Adapter 协议",
        draft.execution_status == "stage0-adapter-only" or adapter_ready,
        "可执行配置必须声明 looper-adapter/v1，并通过 normalize 阶段生成标准输出。",
    ))
    managed_local_process = bool(
        draft.runtime_type == "local-process"
        and spec.get("trust") == "trusted"
        and package_ready
        and managed_provisioning
        and provisioning_ok
    )
    install_safe = draft.execution_status == "stage0-adapter-only" or bool(
        draft.execution_status == "executable"
        and adapter_ready
        and (
            (draft.runtime_type == "container" and pinned_image)
            or managed_local_process
        )
    )
    constraints.append(_constraint(
        "execution.install-boundary", "执行", "导入配置不会执行宿主机代码",
        install_safe,
        "Stage 0 可直接登记；可执行配置需要 digest 固定容器，或包含幂等自动部署的受信任 ZIP。",
    ))

    policy = runtime.get("executionPolicy") or {}
    network_policy = policy.get("network") or {}
    storage_policy = policy.get("storage") or {}
    placement_policy = policy.get("placement") or {}
    evidence_policy = policy.get("environmentEvidence") or {}
    declared_inputs = {item.get("id"): item for item in adapter.get("inputs", [])}
    storage_input_id = storage_policy.get("inputId")
    storage_input = declared_inputs.get(storage_input_id)
    network_consistent = (
        runtime.get("networkMode", "none") == "none"
        and network_policy.get("mode") == "none"
        and not network_policy.get("allowedHosts")
        and network_policy.get("maxTransferBytes") is None
    ) or (
        runtime.get("networkMode") == "bridge"
        and network_policy.get("mode") == "restricted-egress"
        and bool(network_policy.get("allowedHosts"))
        and isinstance(network_policy.get("maxTransferBytes"), int)
    )
    storage_consistent = (
        storage_policy.get("mode") == "workspace"
        and storage_input_id is None
        and not storage_policy.get("destructive")
    ) or (
        storage_policy.get("mode") == "bound-input"
        and bool(storage_input)
        and storage_input.get("kind") == "device"
        and storage_input.get("required") is True
    )
    policy_ready = bool(
        (managed_local_process and runtime.get("dependencyLockDigest"))
        or (
            policy
            and runtime.get("dependencyLockDigest")
            and placement_policy.get("mode") == "isolated-container"
            and runtime.get("type") == "container"
            and network_consistent
            and storage_consistent
            and evidence_policy.get("profile") == "looper.system-fingerprint/v1alpha1"
            and evidence_policy.get("requiredFields")
        )
    )
    constraints.append(_constraint(
        "execution.production-policy", "执行", "生产执行策略完整且可机读",
        draft.execution_status == "stage0-adapter-only" or policy_ready,
        "Executable 必须固定 dependency lock；容器需声明完整策略，"
        "受信任本地进程需使用含自动部署声明的完整 ZIP。",
    ))
    input_ids = [item.get("id") for item in adapter.get("inputs", [])]
    input_contract_ok = len(input_ids) == len(set(input_ids)) and all(input_ids)
    constraints.append(_constraint(
        "execution.input-contract", "执行", "命名输入没有歧义且可在运行前绑定",
        draft.execution_status == "stage0-adapter-only" or bool(input_contract_ok),
        "Adapter input ID 必须唯一；设备存储必须引用 required device 输入，secret 只传引用。",
    ))

    required_artifacts = [
        item for item in (spec.get("outputs") or {}).get("artifacts", []) if item.get("required")
    ]
    raw_artifacts = [
        item for item in required_artifacts
        if item.get("role") in {"raw-result", "trace", "profile", "dataset", "histogram"}
    ]
    evidence_ok = bool(
        primary
        and int(primary.get("minimumSamples", 1)) >= draft.minimum_samples
        and draft.repeats >= 3
        and required_artifacts
        and (not draft.retains_raw_evidence or raw_artifacts)
    )
    constraints.append(_constraint(
        "evidence.minimum", "证据", "样本、重复运行和必需 artifact 满足证据下限",
        evidence_ok,
        "主指标 minimumSamples 不得低于页面声明；repeats 至少 3；"
        "原始证据声明必须有对应必需 artifact。",
    ))
    constraints.append(_constraint(
        "audit.cross-environment", "审计", "Base/Reference 与跨环境审计计划已声明",
        draft.has_reference and draft.cross_environment_audit,
        "这不再阻止进入场景目录；正式选型准入前仍须生成 Reference Validity、Rank Stability、"
        "Task Leverage 和 Environment Sensitivity。可在 spec.audit 中声明默认计划。",
        blocking=False,
    ))
    constraints.append(_constraint(
        "trust.local-approval", "信任", "注册不自动授予本地执行信任",
        spec.get("trust") != "trusted" if spec else True,
        "manifest 即使声明 trusted，注册后仍保持未审批；本地安装需要独立人工批准。",
        blocking=False,
    ))
    digest = canonical_digest(manifest) if manifest_error is None and manifest is not None else None
    if session is not None:
        benchmark_key = f"{draft.benchmark_id}@{draft.version}"
        version_owner = session.get(BenchmarkRecord, benchmark_key)
        bootstrap_owner = _is_bootstrap_catalog_record(version_owner)
        version_available = version_owner is None or bootstrap_owner
        constraints.insert(2, _constraint(
            "identity.version-available",
            "身份",
            "Benchmark ID 与版本尚未被占用，或正在升级 source-seeded catalog entry",
            version_available,
            (
                f"{benchmark_key} 可以登记。"
                if version_owner is None
                else (
                    f"{benchmark_key} 是 source-seeded catalog entry；"
                    "导入已批准 ZIP 后将原地升级并保留该身份。"
                    if bootstrap_owner
                    else f"{benchmark_key} 已存在；请提升版本号。新版本会成为目录中的当前版本，"
                    "旧版本仅供已有实验追溯。"
                )
            ),
        ))
        if digest is not None:
            digest_owner = session.scalar(
                select(BenchmarkRecord).where(BenchmarkRecord.manifest_digest == digest)
            )
            digest_available = digest_owner is None or (
                bootstrap_owner and digest_owner is version_owner
            )
            constraints.insert(5, _constraint(
                "contract.digest-available",
                "合同",
                "manifest 内容不是已登记版本的重复副本，或正在升级 source-seeded catalog entry",
                digest_available,
                (
                    "manifest 摘要尚未登记。"
                    if digest_owner is None
                    else (
                        f"配置内容与 {digest_owner.key} 相同；"
                        "该 source-seeded 条目可由 ZIP 原地升级。"
                        if digest_available
                        else f"配置内容与 {digest_owner.key} 完全相同；请确认版本和内容确实有变化。"
                    )
                ),
            ))
    return constraints, digest


def draft_from_manifest_bytes(
    raw: bytes,
    *,
    filename: str,
) -> BenchmarkRegistrationDraft:
    if not raw:
        raise RegistrationError(
            "benchmark configuration file is empty",
            status_code=422,
            code="empty_benchmark_configuration",
        )
    if len(raw) > 2 * 1024 * 1024:
        raise RegistrationError(
            "benchmark configuration exceeds the 2 MiB limit",
            status_code=413,
            code="benchmark_configuration_too_large",
        )
    try:
        text = raw.decode("utf-8")
        document = (
            yaml.safe_load(text)
            if filename.lower().endswith((".yaml", ".yml"))
            else __import__("json").loads(text)
        )
    except (UnicodeDecodeError, ValueError, yaml.YAMLError) as error:
        raise RegistrationError(
            "benchmark configuration must be valid UTF-8 YAML or JSON",
            status_code=422,
            code="invalid_benchmark_configuration",
        ) from error
    if not isinstance(document, dict):
        raise RegistrationError(
            "benchmark configuration root must be an object",
            status_code=422,
            code="invalid_benchmark_configuration",
        )
    try:
        validate_document(document, "benchmark-manifest.schema.json")
    except ManifestError as error:
        raise RegistrationError(
            f"benchmark configuration does not match the schema: {error}",
            status_code=422,
            code="benchmark_configuration_schema_failed",
        ) from error
    metadata = document["metadata"]
    spec = document["spec"]
    source = metadata.get("source") or {}
    scenario = spec.get("scenario") or {}
    adapter = spec.get("adapter") or {}
    primary_metric = str(scenario.get("primary_metric") or adapter.get("primaryMetric") or "")
    primary = spec.get("metrics", {}).get(primary_metric, {})
    runtime = spec["runtime"]
    artifacts = spec["outputs"].get("artifacts", [])
    hard_gate_ids = [
        str(item.get("id"))
        for item in scenario.get("slo_gates", [])
        if item.get("hard", True)
        and item.get("kind") in {"correctness", "safety", "slo"}
        and item.get("id")
    ]
    required_check_ids = [str(item) for item in adapter.get("requiredChecks", [])]
    correctness_parts = []
    if hard_gate_ids:
        correctness_parts.append("hard gates: " + ", ".join(hard_gate_ids))
    if required_check_ids:
        correctness_parts.append("adapter checks: " + ", ".join(required_check_ids))
    audit = spec.get("audit") or {}
    reference_policy = audit.get("referencePolicy")
    environment_axes = set(audit.get("environmentAxes") or [])
    cross_environment_axes = {
        "machine", "day", "placement", "compiler", "runtime", "driver",
        "region", "zone", "network", "storage",
    }
    return BenchmarkRegistrationDraft(
        name=str(metadata["name"]),
        benchmark_id=str(metadata["id"]),
        version=str(metadata["version"]),
        source_url=str(source.get("url") or ""),
        source_revision=str(source.get("commit") or source.get("digest") or ""),
        license=str(metadata["license"]),
        category=str(
            (metadata.get("x-extensions") or {}).get("category")
            or (spec.get("x-extensions") or {}).get("category")
            or "unclassified"
        ),
        execution_model=str(adapter.get("executionModel") or "custom"),
        decision_question=str(scenario.get("decision_question") or ""),
        primary_metric=primary_metric,
        primary_unit=str(primary.get("unit") or ""),
        correctness_contract="; ".join(correctness_parts),
        runtime_type=str(runtime["type"]),
        execution_status=str(
            spec.get("x-extensions", {}).get("executionStatus", "executable")
        ),
        image=str(runtime.get("image") or ""),
        minimum_samples=int(primary.get("minimumSamples", 1)),
        repeats=int(audit.get("minimumRepeats", 3)),
        has_reference=reference_policy in {"required", "recommended"},
        retains_raw_evidence=any(
            item.get("required")
            and item.get("role") in {"raw-result", "trace", "profile", "dataset", "histogram"}
            for item in artifacts
        ),
        cross_environment_audit=bool(environment_axes & cross_environment_axes),
        manifest=document,
    )


def registration_ready(constraints: list[dict[str, Any]]) -> bool:
    return all(item["status"] == "pass" for item in constraints if item["blocking"])


def _event_payload(record: BenchmarkRegistrationRecord) -> dict[str, Any]:
    return {
        "revision": record.revision,
        "draftDigest": canonical_digest(record.draft_json),
        "manifestDigest": record.manifest_digest,
        "packageDigest": record.package_digest,
        "constraintResults": {
            item["code"]: item["status"] for item in record.constraints_json
        },
    }


def registration_view(record: BenchmarkRegistrationRecord) -> dict[str, Any]:
    draft = BenchmarkRegistrationDraft.model_validate(record.draft_json)
    return {
        "id": record.id,
        "status": record.status,
        "revision": record.revision,
        "draft": draft.model_dump(mode="json", by_alias=True),
        "constraints": record.constraints_json,
        "readyToRegister": registration_ready(record.constraints_json),
        "manifestDigest": record.manifest_digest,
        "packageDigest": record.package_digest,
        "packageReady": bool(record.package_path),
        "benchmarkKey": record.benchmark_key,
        "createdAt": record.created_at.isoformat(),
        "updatedAt": record.updated_at.isoformat(),
        "registeredAt": record.registered_at.isoformat() if record.registered_at else None,
    }


def create_registration(
    session: Session,
    draft: BenchmarkRegistrationDraft,
    *,
    package_digest: str | None = None,
    package_path: str | None = None,
) -> BenchmarkRegistrationRecord:
    constraints, manifest_digest = evaluate_registration_constraints(
        draft, session=session, package_ready=bool(package_path)
    )
    now = utc_now()
    record = BenchmarkRegistrationRecord(
        id=new_id("breg"),
        status="draft",
        revision=1,
        draft_json=draft.model_dump(mode="json", by_alias=True),
        constraints_json=constraints,
        manifest_digest=manifest_digest,
        package_digest=package_digest,
        package_path=package_path,
        created_at=now,
        updated_at=now,
    )
    session.add(record)
    session.flush()
    append_event(
        session,
        experiment_id=None,
        event_type="benchmark.registration.created",
        entity_type="benchmark_registration",
        entity_id=record.id,
        idempotency_key=f"benchmark-registration:{record.id}:revision:1",
        payload=_event_payload(record),
    )
    return record


def get_registration(session: Session, registration_id: str) -> BenchmarkRegistrationRecord:
    record = session.get(BenchmarkRegistrationRecord, registration_id)
    if record is None:
        raise RegistrationError(
            "benchmark registration does not exist", status_code=404, code="registration_not_found"
        )
    return record


def update_registration(
    session: Session,
    registration_id: str,
    request: BenchmarkRegistrationUpdate,
) -> BenchmarkRegistrationRecord:
    existing = get_registration(session, registration_id)
    constraints, manifest_digest = evaluate_registration_constraints(
        request.draft,
        session=session,
        package_ready=bool(existing.package_path),
    )
    result = session.execute(
        update(BenchmarkRegistrationRecord)
        .where(
            BenchmarkRegistrationRecord.id == registration_id,
            BenchmarkRegistrationRecord.status == "draft",
            BenchmarkRegistrationRecord.revision == request.expected_revision,
        )
        .values(
            draft_json=request.draft.model_dump(mode="json", by_alias=True),
            constraints_json=constraints,
            manifest_digest=manifest_digest,
            revision=request.expected_revision + 1,
            updated_at=utc_now(),
        )
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        record = get_registration(session, registration_id)
        if record.status != "draft":
            raise RegistrationError(
                "registered benchmark versions are immutable", code="immutable_registration"
            )
        raise RegistrationError("registration revision conflict", code="revision_conflict")
    session.expire_all()
    record = get_registration(session, registration_id)
    append_event(
        session,
        experiment_id=None,
        event_type="benchmark.registration.updated",
        entity_type="benchmark_registration",
        entity_id=record.id,
        idempotency_key=f"benchmark-registration:{record.id}:revision:{record.revision}",
        payload=_event_payload(record),
    )
    return record


def register_benchmark(
    session: Session,
    registration_id: str,
    request: BenchmarkRegistrationRegister,
) -> BenchmarkRegistrationRecord:
    record = get_registration(session, registration_id)
    if record.status != "draft":
        raise RegistrationError("registration is already finalized", code="immutable_registration")
    if record.revision != request.expected_revision:
        raise RegistrationError("registration revision conflict", code="revision_conflict")
    draft = BenchmarkRegistrationDraft.model_validate(record.draft_json)
    constraints, manifest_digest = evaluate_registration_constraints(
        draft, session=session, package_ready=bool(record.package_path)
    )
    record.constraints_json = constraints
    record.manifest_digest = manifest_digest
    if not registration_ready(constraints) or draft.manifest is None or manifest_digest is None:
        raise RegistrationError(
            "benchmark registration constraints are not satisfied",
            code="registration_constraints_failed",
            constraints=constraints,
        )
    claim = session.execute(
        update(BenchmarkRegistrationRecord)
        .where(
            BenchmarkRegistrationRecord.id == registration_id,
            BenchmarkRegistrationRecord.status == "draft",
            BenchmarkRegistrationRecord.revision == request.expected_revision,
        )
        .values(
            status="registering",
            constraints_json=constraints,
            manifest_digest=manifest_digest,
            updated_at=utc_now(),
        )
    )
    if claim.rowcount != 1:  # type: ignore[attr-defined]
        session.expire_all()
        latest = get_registration(session, registration_id)
        if latest.status != "draft":
            raise RegistrationError(
                "registration is already finalized", code="immutable_registration"
            )
        raise RegistrationError("registration revision conflict", code="revision_conflict")
    key = f"{draft.benchmark_id}@{draft.version}"
    existing = session.get(BenchmarkRecord, key)
    bootstrap_upgrade = _is_bootstrap_catalog_record(existing)
    if existing is not None and not bootstrap_upgrade:
        raise RegistrationError(
            "benchmark id and version already exist", code="benchmark_version_exists"
        )
    digest_owner = session.scalar(
        select(BenchmarkRecord).where(BenchmarkRecord.manifest_digest == manifest_digest)
    )
    if digest_owner is not None and not (bootstrap_upgrade and digest_owner is existing):
        raise RegistrationError(
            "manifest digest is already registered", code="manifest_digest_exists"
        )
    now = utc_now()
    manifest = draft.manifest
    metadata = manifest["metadata"]
    trusted_package = bool(
        record.package_path
        and draft.runtime_type == "local-process"
        and manifest["spec"].get("trust") == "trusted"
    )
    if existing is None:
        benchmark = BenchmarkRecord(
            key=key,
            benchmark_id=draft.benchmark_id,
            version=draft.version,
            name=draft.name,
            description=metadata.get("description", ""),
            license=draft.license,
            manifest_digest=manifest_digest,
            manifest_json=manifest,
            manifest_path=record.package_path,
            package_digest=record.package_digest,
            trusted=trusted_package,
            installed_at=now,
        )
        session.add(benchmark)
    else:
        benchmark = existing
        benchmark.name = draft.name
        benchmark.description = metadata.get("description", "")
        benchmark.license = draft.license
        benchmark.manifest_digest = manifest_digest
        benchmark.manifest_json = manifest
        benchmark.manifest_path = record.package_path
        benchmark.package_digest = record.package_digest
        benchmark.trusted = trusted_package
        benchmark.installed_at = now
    session.expire(record)
    record.status = "registered"
    record.benchmark_key = key
    record.registered_at = now
    record.updated_at = now
    record.revision += 1
    session.flush()
    payload = _event_payload(record)
    runtime = manifest["spec"]["runtime"]
    execution_status = manifest["spec"].get("x-extensions", {}).get(
        "executionStatus", "executable"
    )
    payload.update({
        "benchmarkKey": key,
        "trusted": trusted_package,
        "runnable": execution_status == "executable"
        and (trusted_package or runtime.get("type") == "container"),
    })
    append_event(
        session,
        experiment_id=None,
        event_type="benchmark.registration.registered",
        entity_type="benchmark_registration",
        entity_id=record.id,
        idempotency_key=f"benchmark-registration:{record.id}:registered:{record.revision}",
        payload=payload,
    )
    return record
