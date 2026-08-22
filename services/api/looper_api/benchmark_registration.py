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
    category: str = Field(default="cpu-iaas", max_length=80)
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


def evaluate_registration_constraints(
    draft: BenchmarkRegistrationDraft,
) -> tuple[list[dict[str, Any]], str | None]:
    constraints: list[dict[str, Any]] = []
    identifier_ok = bool(re.fullmatch(r"[a-z][a-z0-9.-]{2,63}", draft.benchmark_id))
    constraints.append(_constraint(
        "identity.stable-id", "身份", "Benchmark ID 符合 v1alpha1 稳定标识规则",
        identifier_ok, "必须匹配 ^[a-z][a-z0-9.-]{2,63}$。",
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
        bool(draft.correctness_contract and (hard_gates or required_checks)),
        "页面门禁说明非空，且 scenario.slo_gates 或 adapter.requiredChecks 至少声明一项。",
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
    commands = runtime.get("commands") or {}
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
    install_safe = draft.execution_status == "stage0-adapter-only" or bool(
        draft.execution_status == "executable"
        and draft.runtime_type == "container"
        and pinned_image
        and adapter_ready
    )
    constraints.append(_constraint(
        "execution.install-boundary", "执行", "导入配置不会执行宿主机代码",
        install_safe,
        "Stage 0 可直接登记；可执行配置只允许 digest 固定的容器和通用 Adapter。",
    ))

    required_artifacts = [
        item for item in (spec.get("outputs") or {}).get("artifacts", []) if item.get("required")
    ]
    raw_artifacts = [
        item for item in required_artifacts
        if item.get("role") in {"trace", "profile", "dataset", "histogram"}
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
        "audit.cross-environment", "审计", "Base/Reference 与跨环境审计已声明",
        draft.has_reference and draft.cross_environment_audit,
        "正式准入前必须生成 Reference Validity、Rank Stability、"
        "Task Leverage 和 Environment Sensitivity。",
    ))
    constraints.append(_constraint(
        "trust.local-approval", "信任", "注册不自动授予本地执行信任",
        spec.get("trust") != "trusted" if spec else True,
        "manifest 即使声明 trusted，注册后仍保持未审批；本地安装需要独立人工批准。",
        blocking=False,
    ))
    digest = canonical_digest(manifest) if manifest_error is None and manifest is not None else None
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
    return BenchmarkRegistrationDraft(
        name=str(metadata["name"]),
        benchmark_id=str(metadata["id"]),
        version=str(metadata["version"]),
        source_url=str(source.get("url") or ""),
        source_revision=str(source.get("commit") or source.get("digest") or ""),
        license=str(metadata["license"]),
        category=str(adapter.get("executionModel") or "cpu-iaas"),
        decision_question=str(scenario.get("decision_question") or ""),
        primary_metric=primary_metric,
        primary_unit=str(primary.get("unit") or ""),
        correctness_contract="",
        runtime_type=str(runtime["type"]),
        execution_status=str(
            spec.get("x-extensions", {}).get("executionStatus", "executable")
        ),
        image=str(runtime.get("image") or ""),
        minimum_samples=int(primary.get("minimumSamples", 1)),
        repeats=3,
        has_reference=False,
        retains_raw_evidence=any(
            item.get("required")
            and item.get("role") in {"trace", "profile", "dataset", "histogram"}
            for item in artifacts
        ),
        cross_environment_audit=False,
        manifest=document,
    )


def registration_ready(constraints: list[dict[str, Any]]) -> bool:
    return all(item["status"] == "pass" for item in constraints if item["blocking"])


def _event_payload(record: BenchmarkRegistrationRecord) -> dict[str, Any]:
    return {
        "revision": record.revision,
        "draftDigest": canonical_digest(record.draft_json),
        "manifestDigest": record.manifest_digest,
        "constraintResults": {
            item["code"]: item["status"] for item in record.constraints_json
        },
    }


def registration_view(record: BenchmarkRegistrationRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "status": record.status,
        "revision": record.revision,
        "draft": record.draft_json,
        "constraints": record.constraints_json,
        "readyToRegister": registration_ready(record.constraints_json),
        "manifestDigest": record.manifest_digest,
        "benchmarkKey": record.benchmark_key,
        "createdAt": record.created_at.isoformat(),
        "updatedAt": record.updated_at.isoformat(),
        "registeredAt": record.registered_at.isoformat() if record.registered_at else None,
    }


def create_registration(
    session: Session, draft: BenchmarkRegistrationDraft
) -> BenchmarkRegistrationRecord:
    constraints, manifest_digest = evaluate_registration_constraints(draft)
    now = utc_now()
    record = BenchmarkRegistrationRecord(
        id=new_id("breg"),
        status="draft",
        revision=1,
        draft_json=draft.model_dump(mode="json", by_alias=True),
        constraints_json=constraints,
        manifest_digest=manifest_digest,
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
    constraints, manifest_digest = evaluate_registration_constraints(request.draft)
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
    constraints, manifest_digest = evaluate_registration_constraints(draft)
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
    if session.get(BenchmarkRecord, key) is not None:
        raise RegistrationError(
            "benchmark id and version already exist", code="benchmark_version_exists"
        )
    digest_owner = session.scalar(
        select(BenchmarkRecord).where(BenchmarkRecord.manifest_digest == manifest_digest)
    )
    if digest_owner is not None:
        raise RegistrationError(
            "manifest digest is already registered", code="manifest_digest_exists"
        )
    now = utc_now()
    manifest = draft.manifest
    metadata = manifest["metadata"]
    session.add(BenchmarkRecord(
        key=key,
        benchmark_id=draft.benchmark_id,
        version=draft.version,
        name=draft.name,
        description=metadata.get("description", ""),
        license=draft.license,
        manifest_digest=manifest_digest,
        manifest_json=manifest,
        manifest_path=None,
        trusted=False,
        installed_at=now,
    ))
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
        "trusted": False,
        "runnable": execution_status == "executable" and runtime.get("type") == "container",
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
