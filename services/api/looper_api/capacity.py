"""Auditable capacity-study drafts, preflight checks, and execution contracts."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import threading
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
import yaml
from looper_core.canonical import canonical_digest, new_id, utc_now
from looper_core.contracts import (
    Aggregation,
    BenchmarkInputBinding,
    BudgetSpec,
    Comparison,
    Direction,
    ExperimentalDesign,
    ExperimentCreate,
    ExperimentMode,
    ExperimentSpec,
    GateKind,
    GateScope,
    GateSpec,
    GoodputPolicy,
    LoadSearchSpec,
    ObjectiveSpec,
    Operator,
    ScenarioBenchmarkSpec,
    ScenarioRoleSpec,
    SelectionDesign,
    TailEvidenceSpec,
    TargetBindingSpec,
)
from looper_core.manifest import load_and_validate_manifest
from looper_core.state import ExperimentStatus
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from looper_api.config import Settings
from looper_api.external_targets import open_ssh_client
from looper_api.models import (
    BenchmarkRecord,
    CapacityStudyRecord,
    ExperimentRecord,
    SelectionLoadPointRecord,
    SourceDiscoveryRecord,
    TargetRecord,
)
from looper_api.remote_credentials import EncryptedSshCredentialStore, RemoteCredentialError
from looper_api.source_archive_store import EncryptedSourceArchiveStore, SourceArchiveError
from looper_api.source_discovery import TOOLS, SourceWorkspace


class CapacityError(ValueError):
    status_code = 422
    code = "capacity_study_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 422,
        code: str = "capacity_study_error",
        constraints: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.constraints = constraints


class CapacityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _commit_revisioned_draft(
    session: Session,
    record: CapacityStudyRecord,
    expected_revision: int,
    values: dict[str, Any],
) -> CapacityStudyRecord:
    result = session.execute(
        update(CapacityStudyRecord)
        .where(
            CapacityStudyRecord.id == record.id,
            CapacityStudyRecord.status == "draft",
            CapacityStudyRecord.revision == expected_revision,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        session.expire_all()
        raise CapacityError(
            "capacity draft changed in another session; reload before continuing",
            status_code=409,
            code="capacity_revision_conflict",
        )
    session.expire(record)
    session.refresh(record)
    return record


class BuildEvidence(CapacityModel):
    file: str
    start_line: int = Field(alias="startLine", ge=1)
    end_line: int = Field(alias="endLine", ge=1)


class BuildCheck(CapacityModel):
    id: str
    label: str
    status: Literal["pass", "fixed", "fail"]
    detail: str


class BuildPlan(CapacityModel):
    dockerfile: str = Field(min_length=1, max_length=100_000)
    compose: str = Field(min_length=1, max_length=100_000)
    start_command: str = Field(alias="startCommand", min_length=1, max_length=1000)
    health_path: str = Field(alias="healthPath", pattern=r"^/")
    service_port: int = Field(alias="servicePort", ge=1, le=65535)
    source_root: str = Field(default=".", alias="sourceRoot", max_length=1000)
    dependencies: list[str] = Field(default_factory=list, max_length=100)
    unresolved: list[str] = Field(default_factory=list, max_length=100)
    advisories: list[str] = Field(default_factory=list, max_length=100)
    checks: list[BuildCheck] = Field(default_factory=list, max_length=100)
    ordered_migrations: list[str] = Field(
        default_factory=list, alias="orderedMigrations", max_length=1000
    )
    evidence: list[BuildEvidence] = Field(default_factory=list, max_length=100)
    approved: bool = False

    @field_validator("health_path")
    @classmethod
    def safe_health_path(cls, value: str) -> str:
        if any(character.isspace() or character in {'"', "'", "`", "\\"} for character in value):
            raise ValueError("health path contains unsafe characters")
        return value

    @field_validator("source_root")
    @classmethod
    def safe_source_root(cls, value: str) -> str:
        return _safe_relative_path(value, label="source root").as_posix()

    @field_validator("ordered_migrations")
    @classmethod
    def safe_ordered_migrations(cls, values: list[str]) -> list[str]:
        return [
            _safe_relative_path(value, label="ordered migration").as_posix()
            for value in values
        ]


class ScenarioAssertion(CapacityModel):
    kind: Literal["status", "json-equals", "json-exists"] = "status"
    field: str = ""
    expected: Any = 200


class ScenarioStep(CapacityModel):
    id: str = Field(min_length=1, max_length=100)
    interface_id: str = Field(alias="interfaceId", min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=160)
    method: str = Field(pattern=r"^[A-Z]+$")
    path: str = Field(pattern=r"^/")
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any | None = None
    extract: dict[str, str] = Field(default_factory=dict)
    assertions: list[ScenarioAssertion] = Field(default_factory=list)
    side_effect: str = Field(default="unknown", alias="sideEffect", max_length=80)


class ScenarioPlan(CapacityModel):
    steps: list[ScenarioStep] = Field(default_factory=list, max_length=100)
    reset_strategy: Literal["none", "compose-recreate", "custom"] = Field(
        default="none", alias="resetStrategy"
    )
    reset_command: str = Field(default="", alias="resetCommand", max_length=2000)

    @field_validator("reset_command")
    @classmethod
    def single_line_reset_command(cls, value: str) -> str:
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError("reset command must be a single shell command")
        return value


class SloPlan(CapacityModel):
    minimum_success_rate: float = Field(default=0.999, alias="minimumSuccessRate", gt=0, le=1)
    maximum_error_rate: float = Field(default=0.001, alias="maximumErrorRate", ge=0, le=1)
    maximum_timeout_rate: float = Field(default=0.001, alias="maximumTimeoutRate", ge=0, le=1)
    p99_ms: float = Field(default=500, alias="p99Ms", gt=0)
    p999_ms: float = Field(default=1000, alias="p999Ms", gt=0)
    confidence_level: Literal[0.95] = Field(default=0.95, alias="confidenceLevel")
    minimum_samples: int = Field(default=1000, alias="minimumSamples", ge=100, le=10_000_000)


class TargetPlan(CapacityModel):
    enabled_networks: list[Literal["internal", "external"]] = Field(
        default_factory=lambda: ["internal", "external"],
        alias="enabledNetworks",
        min_length=1,
        max_length=2,
    )
    sut_ids: list[str] = Field(default_factory=list, alias="sutIds", max_length=100)
    internal_load_generator_id: str = Field(default="", alias="internalLoadGeneratorId")
    external_load_generator_id: str = Field(default="", alias="externalLoadGeneratorId")
    internal_base_urls: dict[str, str] = Field(default_factory=dict, alias="internalBaseUrls")
    external_base_urls: dict[str, str] = Field(default_factory=dict, alias="externalBaseUrls")

    @field_validator("enabled_networks")
    @classmethod
    def unique_enabled_networks(
        cls, value: list[Literal["internal", "external"]]
    ) -> list[Literal["internal", "external"]]:
        if len(set(value)) != len(value):
            raise ValueError("enabled networks must be unique")
        return value


class BudgetPlan(CapacityModel):
    max_seconds: int = Field(default=3600, alias="maxSeconds", ge=120, le=604800)
    max_attempts: int = Field(default=80, alias="maxAttempts", ge=1, le=10000)
    cost_cap: float = Field(default=10, alias="costCap", ge=0, le=1_000_000)
    reference_rps: float = Field(default=100, alias="referenceRps", gt=0, le=10_000_000)
    measurement_seconds: int = Field(default=20, alias="measurementSeconds", ge=5, le=3600)


class CapacityDraft(CapacityModel):
    build: BuildPlan
    scenario: ScenarioPlan
    slo: SloPlan = Field(default_factory=SloPlan)
    targets: TargetPlan = Field(default_factory=TargetPlan)
    budget: BudgetPlan = Field(default_factory=BudgetPlan)


class CapacityDraftUpdate(CapacityModel):
    expected_revision: int = Field(alias="expectedRevision", ge=1)
    current_step: int = Field(alias="currentStep", ge=0, le=4)
    draft: CapacityDraft


class CapacityBuildRepairRequest(CapacityModel):
    expected_revision: int = Field(alias="expectedRevision", ge=1)


class CapacityCreateRequest(CapacityModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)


class CapacityStartRequest(CapacityModel):
    expected_revision: int = Field(alias="expectedRevision", ge=1)
    excluded_target_ids: list[str] = Field(default_factory=list, alias="excludedTargetIds")
    acknowledge_partial: bool = Field(default=False, alias="acknowledgePartial")


class BuildAgentOutput(CapacityModel):
    dockerfile: str
    compose: str
    start_command: str = Field(alias="startCommand")
    health_path: str = Field(alias="healthPath")
    service_port: int = Field(alias="servicePort")
    dependencies: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    evidence: list[BuildEvidence]


class CapacityPlanAgentOutput(CapacityModel):
    build: BuildAgentOutput
    scenario: ScenarioPlan
    scenario_rationale: str = Field(alias="scenarioRationale", min_length=1, max_length=4000)


class GeneratedCapacityPlan(CapacityModel):
    build: BuildPlan
    scenario: ScenarioPlan
    scenario_rationale: str = Field(alias="scenarioRationale")
    omitted_interface_ids: list[str] = Field(alias="omittedInterfaceIds")
    scenario_mode: Literal["agent-selected", "deterministic-fallback"] = Field(
        default="agent-selected", alias="scenarioMode"
    )


def _strip_json(content: str) -> str:
    normalized = content.strip()
    if normalized.startswith("```"):
        first_newline = normalized.find("\n")
        if first_newline != -1 and normalized.endswith("```"):
            normalized = normalized[first_newline + 1 : -3].strip()
    first = normalized.find("{")
    last = normalized.rfind("}")
    return normalized[first : last + 1] if first != -1 and last > first else normalized


def _load_agent_json(content: str) -> Any:
    """Parse JSON-mode output with a safe fallback for JSON-like mappings."""
    normalized = _strip_json(content)
    try:
        return json.loads(normalized)
    except json.JSONDecodeError as json_error:
        if normalized.startswith("{{") and normalized.endswith("}}"):
            normalized = normalized[1:-1]
            try:
                return json.loads(normalized)
            except json.JSONDecodeError:
                pass
        parsed = yaml.safe_load(normalized)
        if not isinstance(parsed, dict):
            raise json_error
        return parsed


def _normalize_agent_payload(payload: Any) -> Any:
    """Normalize compact assertion shorthands before strict model validation."""
    if not isinstance(payload, dict):
        return payload
    scenario = payload.get("scenario")
    if not isinstance(scenario, dict):
        return payload
    steps = scenario.get("steps")
    if not isinstance(steps, list):
        return payload
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("assertions"), list):
            continue
        normalized: list[Any] = []
        for assertion in step["assertions"]:
            if not isinstance(assertion, dict) or "kind" in assertion:
                normalized.append(assertion)
                continue
            if "status" in assertion:
                normalized.append(
                    {"kind": "status", "field": "status", "expected": assertion["status"]}
                )
                continue
            if "json-exists" in assertion:
                normalized.append(
                    {
                        "kind": "json-exists",
                        "field": assertion["json-exists"],
                        "expected": True,
                    }
                )
                continue
            if "json-equals" in assertion:
                field = assertion["json-equals"]
                expected = assertion.get("expected")
                if isinstance(field, dict) and len(field) == 1:
                    field, expected = next(iter(field.items()))
                normalized.append(
                    {"kind": "json-equals", "field": field, "expected": expected}
                )
                continue
            normalized.append(assertion)
        step["assertions"] = normalized
    return payload


_SCENARIO_VARIABLE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
_BRACED_ROUTE_PARAMETER = re.compile(r"\{[^{}]+\}")
_COLON_ROUTE_PARAMETER = re.compile(r"(?<=/):[A-Za-z_][A-Za-z0-9_]*")


def _scenario_contract_view(contract: dict[str, Any] | None) -> dict[str, Any]:
    interfaces = ((contract or {}).get("spec") or {}).get("interfaces") or []
    fields = (
        "id",
        "method",
        "path",
        "summary",
        "parameters",
        "requestBody",
        "responses",
        "authentication",
        "sideEffect",
        "unresolved",
    )
    return {
        "interfaces": [
            {field: interface.get(field) for field in fields if field in interface}
            for interface in interfaces
        ]
    }


def _scenario_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for entry in value for item in _scenario_strings(entry)]
    if isinstance(value, dict):
        return [item for entry in value.values() for item in _scenario_strings(entry)]
    return []


def _route_shape(value: str) -> str:
    rendered = _SCENARIO_VARIABLE.sub("__PARAM__", value)
    rendered = _BRACED_ROUTE_PARAMETER.sub("__PARAM__", rendered)
    return _COLON_ROUTE_PARAMETER.sub("__PARAM__", rendered)


def _normalize_agent_scenario(
    scenario: ScenarioPlan, contract: dict[str, Any] | None
) -> ScenarioPlan:
    interfaces = {
        str(item.get("id")): item
        for item in _scenario_contract_view(contract)["interfaces"]
        if item.get("id")
    }
    normalized = scenario.model_copy(deep=True)
    for index, step in enumerate(normalized.steps, 1):
        interface = interfaces.get(step.interface_id)
        step.id = f"step-{index}"
        if interface is None:
            continue
        step.method = str(interface.get("method") or step.method).upper()
        step.side_effect = str(interface.get("sideEffect") or step.side_effect)
        if not step.label.strip():
            step.label = str(interface.get("summary") or interface.get("path") or step.path)
    return normalized


def _scenario_plan_errors(
    scenario: ScenarioPlan, contract: dict[str, Any] | None
) -> list[str]:
    interfaces = {
        str(item.get("id")): item
        for item in _scenario_contract_view(contract)["interfaces"]
        if item.get("id")
    }
    errors: list[str] = []
    if not 1 <= len(scenario.steps) <= 8:
        errors.append("scenario must select between 1 and 8 representative interfaces")
    seen_interfaces: set[str] = set()
    available_variables = {"attempt_id", "iteration"}
    has_write = False
    for index, step in enumerate(scenario.steps, 1):
        prefix = f"scenario step {index}"
        interface = interfaces.get(step.interface_id)
        if interface is None:
            errors.append(f"{prefix} references an unknown interfaceId")
            continue
        if step.interface_id in seen_interfaces:
            errors.append(f"{prefix} duplicates interfaceId {step.interface_id}")
        seen_interfaces.add(step.interface_id)
        expected_method = str(interface.get("method") or "").upper()
        expected_path = str(interface.get("path") or "")
        if step.method != expected_method:
            errors.append(f"{prefix} method must remain {expected_method}")
        if _route_shape(step.path) != _route_shape(expected_path):
            errors.append(f"{prefix} path does not match discovered route {expected_path}")
        route_has_parameters = bool(
            _BRACED_ROUTE_PARAMETER.search(expected_path)
            or _COLON_ROUTE_PARAMETER.search(expected_path)
        )
        if route_has_parameters and not _SCENARIO_VARIABLE.search(step.path):
            errors.append(f"{prefix} must bind every path parameter from a prior extraction")

        referenced = {
            name
            for value in _scenario_strings(
                {"path": step.path, "headers": step.headers, "body": step.body}
            )
            for name in _SCENARIO_VARIABLE.findall(value)
        }
        missing = sorted(referenced - available_variables)
        if missing:
            errors.append(f"{prefix} references variables before extraction: {', '.join(missing)}")

        request_body = interface.get("requestBody") or {}
        if request_body.get("required") and step.body in (None, {}):
            errors.append(f"{prefix} requires a concrete request body")

        success_statuses = {
            int(response["statusCode"])
            for response in interface.get("responses") or []
            if str(response.get("statusCode") or "").isdigit()
            and 200 <= int(response["statusCode"]) < 400
        }
        status_assertions = [item for item in step.assertions if item.kind == "status"]
        if not status_assertions:
            errors.append(f"{prefix} must assert a successful HTTP status")
        else:
            try:
                asserted_status = int(status_assertions[0].expected)
            except (TypeError, ValueError):
                errors.append(f"{prefix} status assertion must be an integer")
            else:
                if success_statuses and asserted_status not in success_statuses:
                    errors.append(
                        f"{prefix} status assertion is not a documented success response"
                    )

        authentication = " ".join(str(item) for item in interface.get("authentication") or [])
        if "bearer" in authentication.casefold():
            authorization = next(
                (value for key, value in step.headers.items() if key.casefold() == "authorization"),
                "",
            )
            if not authorization or (
                not _SCENARIO_VARIABLE.search(authorization)
                and not authorization.startswith("secret://")
            ):
                errors.append(f"{prefix} must bind bearer authentication from a prior token")

        for name, field in step.extract.items():
            if not name.strip() or not field.strip():
                errors.append(f"{prefix} contains an empty response extraction")
            else:
                available_variables.add(name)
        has_write = has_write or (
            step.method not in {"GET", "HEAD", "OPTIONS"}
            or step.side_effect not in {"none", "read"}
        )
    if has_write and scenario.reset_strategy == "none":
        errors.append("a scenario containing writes must select an isolated reset strategy")
    if scenario.reset_strategy == "custom" and not scenario.reset_command.strip():
        errors.append("custom reset strategy requires a command")
    return list(dict.fromkeys(errors))


def _contract_body_example(interface: dict[str, Any]) -> dict[str, str]:
    schema = (interface.get("requestBody") or {}).get("schema") or {}
    description = str(schema.get("description") or "")
    match = re.search(r"\{([^{}]+)\}", description)
    fields = [item.strip() for item in match.group(1).split(",")] if match else []
    body: dict[str, str] = {}
    for field in fields:
        lowered = field.casefold()
        if "email" in lowered:
            body[field] = "looper_{{attempt_id}}_{{iteration}}@example.invalid"
        elif "password" in lowered:
            body[field] = "LooperTest!{{attempt_id}}-{{iteration}}"
        elif "username" in lowered or lowered == "name":
            body[field] = "looper_{{attempt_id}}_{{iteration}}"
        else:
            body[field] = f"{field}-{{{{attempt_id}}}}-{{{{iteration}}}}"
    return body


def _fallback_representative_scenario(contract: dict[str, Any] | None) -> ScenarioPlan:
    interfaces = _scenario_contract_view(contract)["interfaces"]
    register = next(
        (
            item
            for item in interfaces
            if item.get("method") == "POST"
            and "register" in f"{item.get('path', '')} {item.get('summary', '')}".casefold()
        ),
        None,
    )
    bearer_read = next(
        (
            item
            for item in interfaces
            if item.get("method") == "GET"
            and "bearer" in " ".join(item.get("authentication") or []).casefold()
            and not _BRACED_ROUTE_PARAMETER.search(str(item.get("path") or ""))
            and not _COLON_ROUTE_PARAMETER.search(str(item.get("path") or ""))
        ),
        None,
    )
    selected = [item for item in (register, bearer_read) if item is not None]
    if not selected:
        selected = [
            item
            for item in interfaces
            if item.get("method") == "GET"
            and not (item.get("authentication") or [])
            and not _BRACED_ROUTE_PARAMETER.search(str(item.get("path") or ""))
            and not _COLON_ROUTE_PARAMETER.search(str(item.get("path") or ""))
        ][:1]
    steps: list[ScenarioStep] = []
    for index, interface in enumerate(selected, 1):
        responses = interface.get("responses") or []
        expected = next(
            (
                int(item["statusCode"])
                for item in responses
                if str(item.get("statusCode") or "").isdigit()
                and 200 <= int(item["statusCode"]) < 400
            ),
            200,
        )
        is_register = interface is register
        headers = (
            {"Authorization": "Bearer {{access_token}}"}
            if "bearer" in " ".join(interface.get("authentication") or []).casefold()
            else {}
        )
        assertions = [ScenarioAssertion(kind="status", field="status", expected=expected)]
        extract = {"access_token": "accessToken"} if is_register else {}
        if is_register:
            assertions.append(
                ScenarioAssertion(kind="json-exists", field="accessToken", expected=True)
            )
        steps.append(
            ScenarioStep(
                id=f"step-{index}",
                interfaceId=str(interface["id"]),
                label=str(interface.get("summary") or interface.get("path")),
                method=str(interface.get("method") or "GET"),
                path=str(interface.get("path") or "/"),
                headers=headers,
                body=_contract_body_example(interface)
                if interface.get("requestBody")
                else None,
                extract=extract,
                assertions=assertions,
                sideEffect=str(interface.get("sideEffect") or "unknown"),
            )
        )
    if not steps:
        raise CapacityError(
            "no safe representative interface could be selected",
            status_code=422,
            code="capacity_scenario_unavailable",
        )
    return ScenarioPlan(
        steps=steps,
        resetStrategy="compose-recreate"
        if any(step.method not in {"GET", "HEAD", "OPTIONS"} for step in steps)
        else "none",
    )


def _validated_agent_build(
    output: BuildAgentOutput, workspace: SourceWorkspace
) -> BuildPlan:
    for evidence in output.evidence:
        source = workspace.files.get(evidence.file)
        line_count = len(source.splitlines()) if source is not None else 0
        if (
            source is None
            or evidence.end_line < evidence.start_line
            or evidence.end_line > line_count
        ):
            raise ValueError("invalid build-plan evidence")
    build = BuildPlan(**output.model_dump(by_alias=True), approved=False)
    safety = _build_plan_constraints(build)
    if safety:
        build.unresolved.extend(item for item in safety if item not in build.unresolved)
    return build


def _build_plan_constraints(plan: BuildPlan) -> list[str]:
    failures: list[str] = []
    try:
        document = yaml.safe_load(plan.compose)
    except yaml.YAMLError:
        return ["Compose YAML must parse before it can be approved"]
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict) or not services:
        return ["Compose document must declare services"]

    def add(message: str) -> None:
        if message not in failures:
            failures.append(message)

    def normalized(value: Any) -> str:
        return str(value or "").strip().casefold()

    for service in services.values():
        if not isinstance(service, dict):
            add("Compose services must be mappings")
            continue
        if service.get("privileged") is True or normalized(service.get("privileged")) in {
            "1",
            "true",
            "yes",
        }:
            add("Compose may not use privileged containers")
        if normalized(service.get("network_mode")) == "host":
            add("Compose may not use the host network")
        if normalized(service.get("pid")) == "host":
            add("Compose may not join the host PID namespace")
        if normalized(service.get("ipc")) == "host":
            add("Compose may not join the host IPC namespace")
        if service.get("container_name"):
            add("Compose may not reserve a global container name")
        if any(service.get(key) for key in ("cap_add", "devices", "device_cgroup_rules")):
            add("Compose may not add host capabilities or devices")
        if service.get("security_opt"):
            add("Compose may not override container security policy")
        build = service.get("build")
        if isinstance(build, dict) and build.get("ssh"):
            add("Compose builds may not forward the host SSH agent")

        for volume in service.get("volumes") or []:
            source = ""
            volume_type = ""
            if isinstance(volume, str):
                source = volume.split(":", 1)[0].strip()
                volume_type = "bind" if source.startswith(("/", "~")) else ""
            elif isinstance(volume, dict):
                source = str(volume.get("source") or volume.get("src") or "").strip()
                volume_type = normalized(volume.get("type"))
            if "docker.sock" in source.casefold():
                add("Compose may not mount the Docker socket")
            if source.startswith(("/", "~")) or (
                volume_type == "bind" and not source.startswith(("./", "../"))
            ):
                add("Compose may not bind arbitrary host paths")
    return failures


def _safe_relative_path(value: str, *, label: str) -> PurePosixPath:
    normalized = value.replace("\\", "/").strip()
    if normalized in {"", "."}:
        return PurePosixPath(".")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"{label} must be a safe relative path")
    return path


def _workspace_common_root(workspace: SourceWorkspace) -> str:
    directories = [PurePosixPath(path).parts[:-1] for path in workspace.files]
    if not directories:
        return "."
    common: list[str] = []
    for parts in zip(*directories, strict=False):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    return PurePosixPath(*common).as_posix() if common else "."


def _workspace_has_path(workspace: SourceWorkspace, path: PurePosixPath) -> bool:
    prefix = path.as_posix().rstrip("/")
    return any(item == prefix or item.startswith(prefix + "/") for item in workspace.files)


def _compose_build_target(plan: BuildPlan) -> tuple[PurePosixPath, PurePosixPath]:
    document = yaml.safe_load(plan.compose)
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        raise ValueError("Compose must declare a services mapping")
    build_services = [
        value
        for value in services.values()
        if isinstance(value, dict) and "build" in value
    ]
    if len(build_services) != 1:
        raise ValueError("Compose must declare exactly one source-built application service")
    build = build_services[0]["build"]
    if isinstance(build, str):
        context, dockerfile = build, "Dockerfile"
    elif isinstance(build, dict):
        context = str(build.get("context") or ".")
        dockerfile = str(build.get("dockerfile") or "Dockerfile")
    else:
        raise ValueError("Compose build must be a path or mapping")
    return (
        _safe_relative_path(context, label="Compose build context"),
        _safe_relative_path(dockerfile, label="Compose Dockerfile"),
    )


_CREATE_TABLE = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:[A-Za-z_][\w$]*\.)?\"?([A-Za-z_][\w$]*)\"?",
    re.IGNORECASE,
)
_REFERENCE_TABLE = re.compile(
    r"\bREFERENCES\s+(?:[A-Za-z_][\w$]*\.)?\"?([A-Za-z_][\w$]*)\"?",
    re.IGNORECASE,
)


def _ordered_sql_files(files: dict[str, str]) -> tuple[list[str], list[str]]:
    table_owner: dict[str, str] = {}
    for path, content in files.items():
        for table in _CREATE_TABLE.findall(content):
            table_owner[table.casefold()] = path
    dependencies: dict[str, set[str]] = {path: set() for path in files}
    for path, content in files.items():
        for table in _REFERENCE_TABLE.findall(content):
            owner = table_owner.get(table.casefold())
            if owner and owner != path:
                dependencies[path].add(owner)
    ordered: list[str] = []
    remaining = {path: set(values) for path, values in dependencies.items()}
    while remaining:
        ready = sorted(path for path, values in remaining.items() if not values)
        if not ready:
            cycle = sorted(remaining)
            return [], [f"SQL migration dependency cycle: {', '.join(cycle[:8])}"]
        for path in ready:
            ordered.append(path)
            remaining.pop(path)
        for values in remaining.values():
            values.difference_update(ready)
    return ordered, []


def run_build_plan_script(workspace: SourceWorkspace, source_plan: BuildPlan) -> BuildPlan:
    """Normalize and statically validate an Agent plan without executing uploaded code."""
    plan = source_plan.model_copy(deep=True)
    plan.approved = False
    plan.advisories = list(dict.fromkeys([*plan.advisories, *plan.unresolved]))
    plan.unresolved = []
    plan.checks = []
    plan.ordered_migrations = []

    try:
        root = _safe_relative_path(plan.source_root, label="source root")
    except ValueError:
        root = PurePosixPath(_workspace_common_root(workspace))
        plan.source_root = root.as_posix()
        plan.checks.append(
            BuildCheck(
                id="source-root",
                label="源码根目录",
                status="fixed",
                detail=f"脚本将构建根目录修正为 {plan.source_root}",
            )
        )
    else:
        common_root = _workspace_common_root(workspace)
        if plan.source_root == "." and common_root != ".":
            root = PurePosixPath(common_root)
            plan.source_root = common_root
            status, detail = "fixed", f"ZIP 顶层目录已识别为 {common_root}"
        elif plan.source_root == ".":
            status, detail = "pass", "源码文件位于 ZIP 根目录"
        elif _workspace_has_path(workspace, root):
            status, detail = "pass", f"源码根目录 {plan.source_root} 存在"
        else:
            status, detail = "fail", f"源码根目录 {plan.source_root} 不存在"
            plan.unresolved.append(detail)
        plan.checks.append(
            BuildCheck(id="source-root", label="源码根目录", status=status, detail=detail)
        )

    safety = _build_plan_constraints(plan)
    plan.unresolved.extend(item for item in safety if item not in plan.unresolved)
    plan.checks.append(
        BuildCheck(
            id="compose-safety",
            label="容器隔离策略",
            status="fail" if safety else "pass",
            detail="；".join(safety) if safety else "未发现特权容器、宿主网络或危险挂载",
        )
    )

    try:
        context, _dockerfile = _compose_build_target(plan)
        build_root = root / context if plan.source_root != "." else context
        if context.as_posix() != "." and not _workspace_has_path(workspace, build_root):
            raise ValueError(f"Compose build context {context.as_posix()} does not exist")
        document = yaml.safe_load(plan.compose)
        plan.checks.append(
            BuildCheck(
                id="compose-model",
                label="Compose 模型",
                status="pass",
                detail="YAML 可解析且只有一个源码构建服务",
            )
        )
    except (ValueError, yaml.YAMLError) as error:
        message = str(error)[:1000]
        plan.unresolved.append(message)
        plan.checks.append(
            BuildCheck(id="compose-model", label="Compose 模型", status="fail", detail=message)
        )
        return plan

    services = document.get("services") if isinstance(document, dict) else {}
    migration_fixed = False
    for service in services.values():
        if not isinstance(service, dict):
            continue
        volumes = service.get("volumes")
        if not isinstance(volumes, list):
            continue
        for index, volume in enumerate(volumes):
            if not isinstance(volume, str):
                continue
            parts = volume.split(":")
            if len(parts) < 2 or parts[1] != "/docker-entrypoint-initdb.d":
                continue
            mounted = parts[0].removeprefix("./")
            if not mounted or mounted.startswith("/"):
                continue
            relative_prefix = PurePosixPath(mounted)
            archive_prefix = root / relative_prefix if plan.source_root != "." else relative_prefix
            archive_prefix_text = archive_prefix.as_posix()
            sql_files = {
                (
                    path.removeprefix(plan.source_root.rstrip("/") + "/")
                    if plan.source_root != "."
                    else path
                ): content
                for path, content in workspace.files.items()
                if path.casefold().endswith(".sql")
                and (
                    path == archive_prefix_text
                    or path.startswith(archive_prefix_text + "/")
                )
            }
            if not sql_files:
                continue
            ordered, errors = _ordered_sql_files(sql_files)
            if errors:
                plan.unresolved.extend(errors)
                plan.checks.append(
                    BuildCheck(
                        id="migration-order",
                        label="数据库迁移顺序",
                        status="fail",
                        detail="；".join(errors),
                    )
                )
                continue
            plan.ordered_migrations = ordered
            volumes[index] = "./.looper-capacity-migrations:/docker-entrypoint-initdb.d:ro"
            migration_fixed = True
    if migration_fixed:
        plan.compose = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
        plan.checks.append(
            BuildCheck(
                id="migration-order",
                label="数据库迁移顺序",
                status="fixed",
                detail=f"已按外键依赖排列 {len(plan.ordered_migrations)} 个 SQL 文件",
            )
        )
    elif not any(check.id == "migration-order" for check in plan.checks):
        plan.checks.append(
            BuildCheck(
                id="migration-order",
                label="数据库迁移顺序",
                status="pass",
                detail="未发现需要重排的 PostgreSQL 初始化目录",
            )
        )
    plan.unresolved = list(dict.fromkeys(plan.unresolved))
    return plan


async def run_build_plan_harness(
    workspace: SourceWorkspace,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    *,
    previous_plan: BuildPlan | None = None,
    interface_contract: dict[str, Any] | None = None,
) -> BuildPlan | GeneratedCapacityPlan:
    if not settings.deepseek_api_key.strip():
        raise CapacityError(
            "DeepSeek is not configured for build-plan generation",
            status_code=503,
            code="deepseek_not_configured",
        )
    repair_context = ""
    if previous_plan is not None:
        repair_context = (
            "\n\nA previous generated plan was blocked. Produce a complete replacement plan, "
            "not a prose patch. Resolve every prior issue only when source evidence supports "
            "the fix; keep any issue that cannot be proven in unresolved. Never claim a problem "
            "is fixed merely by removing it from unresolved. Previous plan:\n"
            + previous_plan.model_dump_json(by_alias=True)
        )
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are a deployment and HTTP business-scenario planning agent. Inspect source "
                "only through the supplied read-only tools. Do not execute code. Produce a "
                "minimal isolated Dockerfile and Docker Compose plan for a disposable "
                "capacity-test environment. Never "
                "use privileged, host network/PID, Docker socket, or absolute host bind mounts. "
                "Also select one representative executable business iteration from the supplied "
                "interface contract instead of copying every discovered endpoint. Prefer 2-6 "
                "steps: create or authenticate when necessary, extract tokens and resource IDs, "
                "then perform representative reads or bounded writes. Avoid password reset, email, "
                "administrative, destructive, duplicate, and unbindable path-parameter endpoints. "
                "Fill concrete JSON bodies. Use {{attempt_id}} and {{iteration}} to make created "
                "identities unique. Bind response data with extract and reuse it as "
                "{{variable}} in later paths, headers, or bodies. Bearer endpoints must use an "
                "Authorization header "
                "fed by a prior token extraction. Every step must include a documented successful "
                "status assertion and useful json-exists/json-equals assertions when supported. "
                "Every assertion must use {kind,field,expected}; for example "
                "{\"kind\":\"status\",\"field\":\"status\",\"expected\":200} and "
                "{\"kind\":\"json-exists\",\"field\":\"accessToken\",\"expected\":true}. "
                "Select compose-recreate when any step writes. Do not invent missing commands or "
                "request fields: inspect their source and report deployment uncertainty in "
                "build.unresolved. Return one JSON object shaped exactly as "
                "{build:{dockerfile,compose,startCommand,healthPath,servicePort,dependencies,"
                "unresolved,evidence:[{file,startLine,endLine}]},scenario:{steps:[{id,"
                "interfaceId,label,method,path,headers,body,extract,assertions,sideEffect}],"
                "resetStrategy,resetCommand},scenarioRationale}."
            ),
        },
        {
            "role": "user",
            "content": (
                "Determine how this application is built and started for an isolated HTTP "
                "capacity test. Inspect dependency manifests, existing container files, server "
                "entrypoints and health endpoints. Then inspect request DTOs, authentication "
                "responses, and controller behavior needed to make the selected scenario "
                "executable. "
                "Discovered interface contract:\n"
                + json.dumps(_scenario_contract_view(interface_contract), ensure_ascii=False)
                + "\nReturn JSON only after reading source files."
                + repair_context
            ),
        },
    ]
    trace_count = 0
    repairs = 0
    last_valid_build: BuildPlan | None = None
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15))
    try:
        for round_number in range(1, settings.source_discovery_max_tool_rounds + 1):
            response = await http.post(
                f"{str(settings.deepseek_base_url).rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                json={
                    "model": settings.deepseek_model,
                    "messages": messages,
                    "tools": TOOLS,
                    "tool_choice": "required" if round_number == 1 else "auto",
                    "thinking": {"type": "disabled"},
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                    "max_tokens": settings.source_discovery_max_output_tokens,
                },
            )
            if response.status_code >= 400:
                raise CapacityError(
                    f"DeepSeek build-plan request failed with HTTP {response.status_code}",
                    status_code=502,
                    code="deepseek_build_plan_failed",
                )
            try:
                choice = response.json()["choices"][0]
                message = choice["message"]
            except (ValueError, KeyError, IndexError, TypeError) as error:
                raise CapacityError(
                    "DeepSeek returned an invalid build-plan response",
                    status_code=502,
                    code="deepseek_invalid_response",
                ) from error
            calls = message.get("tool_calls") or []
            if calls:
                messages.append(
                    {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
                )
                for call in calls:
                    arguments: dict[str, Any] = {}
                    try:
                        name = call["function"]["name"]
                        arguments = json.loads(call["function"].get("arguments") or "{}")
                        result, _metadata = workspace.tool(name, arguments)
                    except Exception as error:
                        result = {"error": str(error)}
                    trace_count += 1
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", "missing"),
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                continue
            content = message.get("content")
            if not isinstance(content, str) or not trace_count:
                raise CapacityError(
                    "DeepSeek did not inspect source before producing a build plan",
                    status_code=502,
                    code="deepseek_skipped_source_tools",
                )
            try:
                normalized_payload = _normalize_agent_payload(_load_agent_json(content))
                if isinstance(normalized_payload, dict) and isinstance(
                    normalized_payload.get("build"), dict
                ):
                    with suppress(ValidationError, ValueError):
                        last_valid_build = _validated_agent_build(
                            BuildAgentOutput.model_validate(normalized_payload["build"]),
                            workspace,
                        )
                output = CapacityPlanAgentOutput.model_validate(
                    normalized_payload
                )
                build = _validated_agent_build(output.build, workspace)
                scenario = _normalize_agent_scenario(output.scenario, interface_contract)
                scenario_errors = _scenario_plan_errors(scenario, interface_contract)
                if scenario_errors:
                    raise ValueError("; ".join(scenario_errors))
                selected = {step.interface_id for step in scenario.steps}
                omitted = [
                    str(item["id"])
                    for item in _scenario_contract_view(interface_contract)["interfaces"]
                    if item.get("id") and str(item["id"]) not in selected
                ]
                return GeneratedCapacityPlan(
                    build=build,
                    scenario=scenario,
                    scenarioRationale=output.scenario_rationale,
                    omittedInterfaceIds=omitted,
                )
            except (
                ValidationError,
                ValueError,
                json.JSONDecodeError,
                yaml.YAMLError,
            ) as error:
                if repairs < 2:
                    repairs += 1
                    messages.extend(
                        [
                            {"role": "assistant", "content": content},
                            {
                                "role": "user",
                                "content": (
                                    "The JSON or evidence failed validation. Re-read relevant "
                                    "files and return a corrected JSON object only. "
                                    f"Validation error: {str(error)[:800]}"
                                ),
                            },
                        ]
                    )
                    continue
                if last_valid_build is not None:
                    scenario = _fallback_representative_scenario(interface_contract)
                    scenario_errors = _scenario_plan_errors(scenario, interface_contract)
                    if not scenario_errors:
                        selected = {step.interface_id for step in scenario.steps}
                        omitted = [
                            str(item["id"])
                            for item in _scenario_contract_view(interface_contract)[
                                "interfaces"
                            ]
                            if item.get("id") and str(item["id"]) not in selected
                        ]
                        return GeneratedCapacityPlan(
                            build=last_valid_build,
                            scenario=scenario,
                            scenarioRationale=(
                                "Agent 构建方案已通过验证；业务链路因模型输出合同连续失败，"
                                "已从接口合同确定性生成最小认证读链路。"
                            ),
                            omittedInterfaceIds=omitted,
                            scenarioMode="deterministic-fallback",
                        )
                raise CapacityError(
                    "DeepSeek build plan failed contract validation: "
                    f"{str(error)[:600]}; output starts with {content[:240]!r}",
                    status_code=502,
                    code="deepseek_build_plan_invalid",
                ) from error
        raise CapacityError(
            "DeepSeek exceeded the build-plan tool round limit",
            status_code=502,
            code="deepseek_round_limit",
        )
    except httpx.HTTPError as error:
        raise CapacityError(
            "DeepSeek could not be reached for build-plan generation",
            status_code=502,
            code="deepseek_unreachable",
        ) from error
    finally:
        if owns_client:
            await http.aclose()


def _default_steps(discovery: SourceDiscoveryRecord) -> list[ScenarioStep]:
    contract = discovery.contract_json or {}
    interfaces = (contract.get("spec") or {}).get("interfaces") or []
    steps = []
    for index, interface in enumerate(interfaces):
        responses = interface.get("responses") or []
        expected = next(
            (
                int(item["statusCode"])
                for item in responses
                if str(item.get("statusCode", "")).isdigit()
                and 200 <= int(item["statusCode"]) < 400
            ),
            200,
        )
        steps.append(
            ScenarioStep(
                id=f"step-{index + 1}",
                interfaceId=str(interface["id"]),
                label=str(interface.get("summary") or interface.get("path") or f"接口 {index + 1}"),
                method=str(interface.get("method") or "GET").upper(),
                path=str(interface.get("path") or "/"),
                headers={},
                body={} if interface.get("requestBody") else None,
                extract={},
                assertions=[ScenarioAssertion(kind="status", expected=expected)],
                sideEffect=str(interface.get("sideEffect") or "unknown"),
            )
        )
    return steps


def _retained_source_archive(discovery: SourceDiscoveryRecord, settings: Settings) -> bytes:
    retained_until = discovery.archive_retained_until
    now = utc_now()
    comparable_now = (
        now.replace(tzinfo=None)
        if retained_until is not None and retained_until.tzinfo is None
        else now
    )
    if (
        discovery.archive_deleted_at is not None
        or retained_until is None
        or retained_until <= comparable_now
    ):
        raise SourceArchiveError(
            "source archive retention expired; re-upload the same source digest before building"
        )
    return EncryptedSourceArchiveStore(settings).load(discovery.id)


async def create_capacity_study(
    session: Session,
    discovery: SourceDiscoveryRecord,
    settings: Settings,
    *,
    name: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> CapacityStudyRecord:
    if discovery.status != "completed" or not (discovery.contract_json or {}).get("spec", {}).get(
        "interfaces"
    ):
        raise CapacityError("capacity testing requires a completed non-empty interface contract")
    archive = _retained_source_archive(discovery, settings)
    workspace = SourceWorkspace.from_zip(archive, settings)
    generated = await run_build_plan_harness(
        workspace,
        settings,
        client,
        interface_contract=discovery.contract_json,
    )
    if isinstance(generated, GeneratedCapacityPlan):
        source_build = generated.build
        scenario = generated.scenario
        scenario_generation = {
            "mode": f"{generated.scenario_mode}+script-validated",
            "provider": "deepseek",
            "model": settings.deepseek_model,
            "selectedInterfaceCount": len(scenario.steps),
            "discoveredInterfaceCount": len(
                ((discovery.contract_json or {}).get("spec") or {}).get("interfaces") or []
            ),
            "omittedInterfaceIds": generated.omitted_interface_ids,
            "rationale": generated.scenario_rationale,
        }
    else:
        # Compatibility fallback for deterministic test doubles and older internal callers.
        # The production Agent contract always returns GeneratedCapacityPlan.
        source_build = generated
        scenario = ScenarioPlan(steps=_default_steps(discovery))
        scenario_generation = {
            "mode": "legacy-contract-fallback",
            "provider": "deterministic",
            "selectedInterfaceCount": len(scenario.steps),
            "discoveredInterfaceCount": len(scenario.steps),
            "omittedInterfaceIds": [],
            "rationale": "Agent scenario generation was unavailable to this internal caller.",
        }
    build = run_build_plan_script(workspace, source_build)
    now = utc_now()
    draft = CapacityDraft(build=build, scenario=scenario)
    record = CapacityStudyRecord(
        id=new_id("capacity"),
        discovery_id=discovery.id,
        name=(name or f"{discovery.archive_name} 容量测试")[:160],
        status="draft",
        revision=1,
        current_step=0,
        draft_json=draft.model_dump(mode="json", by_alias=True),
        preflight_json={},
        execution_json={
            "phases": [],
            "runs": [],
            "scenarioGeneration": scenario_generation,
            "buildValidations": [
                {
                    "attempt": 1,
                    "at": now.isoformat(),
                    "mode": "agent-plan+script",
                    "agentUsed": True,
                    "blockers": list(build.unresolved),
                    "advisories": list(build.advisories),
                    "checks": [
                        item.model_dump(mode="json", by_alias=True) for item in build.checks
                    ],
                }
            ],
        },
        report_json=None,
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
        started_at=None,
        completed_at=None,
    )
    session.add(record)
    session.flush()
    return record


async def repair_capacity_build_plan(
    session: Session,
    record: CapacityStudyRecord,
    request: CapacityBuildRepairRequest,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> CapacityStudyRecord:
    if record.status != "draft":
        raise CapacityError("only a draft capacity study can be repaired", status_code=409)
    if record.revision != request.expected_revision:
        raise CapacityError(
            "capacity draft changed in another session; reload before repairing",
            status_code=409,
            code="capacity_revision_conflict",
        )
    discovery = session.get(SourceDiscoveryRecord, record.discovery_id)
    if discovery is None:
        raise CapacityError("source discovery not found", status_code=404)
    draft = CapacityDraft.model_validate(record.draft_json)
    before = list(draft.build.unresolved)
    archive = _retained_source_archive(discovery, settings)
    workspace = SourceWorkspace.from_zip(archive, settings)
    execution = dict(record.execution_json or {})
    runtime_failure = str(execution.get("buildRuntimeFailure") or "").strip()
    repaired = run_build_plan_script(workspace, draft.build)
    if runtime_failure and runtime_failure not in repaired.unresolved:
        repaired.unresolved.append(runtime_failure[:4000])
    agent_used = False
    failure_signature = canonical_digest({"blockers": repaired.unresolved})
    validations = list(execution.get("buildValidations") or [])
    repeated_failure = bool(
        repaired.unresolved
        and validations
        and validations[-1].get("failureSignature") == failure_signature
        and validations[-1].get("agentUsed")
    )
    if repaired.unresolved and not repeated_failure:
        generated_repair = await run_build_plan_harness(
            workspace,
            settings,
            client,
            previous_plan=repaired,
            interface_contract=discovery.contract_json,
        )
        repaired = run_build_plan_script(
            workspace,
            generated_repair.build
            if isinstance(generated_repair, GeneratedCapacityPlan)
            else generated_repair,
        )
        agent_used = True
        failure_signature = canonical_digest({"blockers": repaired.unresolved})
    repaired.approved = False
    draft.build = repaired
    revision_before = record.revision
    revision_after = revision_before + 1
    updated_at = utc_now()
    validations.append(
        {
            "attempt": len(validations) + 1,
            "at": updated_at.isoformat(),
            "mode": "script+agent" if agent_used else "script",
            "agentUsed": agent_used,
            "revisionBefore": revision_before,
            "revisionAfter": revision_after,
            "unresolvedBefore": before,
            "blockers": list(repaired.unresolved),
            "advisories": list(repaired.advisories),
            "checks": [
                item.model_dump(mode="json", by_alias=True) for item in repaired.checks
            ],
            "evidence": [item.model_dump(mode="json", by_alias=True) for item in repaired.evidence],
            "failureSignature": failure_signature if repaired.unresolved else None,
            "provider": "deepseek" if agent_used else "deterministic-script",
            "model": settings.deepseek_model if agent_used else None,
        }
    )
    execution["buildValidations"] = validations
    execution.pop("buildRuntimeFailure", None)
    return _commit_revisioned_draft(
        session,
        record,
        request.expected_revision,
        {
            "revision": revision_after,
            "draft_json": draft.model_dump(mode="json", by_alias=True),
            "preflight_json": {},
            "execution_json": execution,
            "error_code": None,
            "error_message": None,
            "updated_at": updated_at,
        },
    )


def list_capacity_studies(session: Session, limit: int = 100) -> list[CapacityStudyRecord]:
    return list(
        session.scalars(
            select(CapacityStudyRecord).order_by(CapacityStudyRecord.updated_at.desc()).limit(limit)
        )
    )


def update_capacity_study(
    session: Session, record: CapacityStudyRecord, request: CapacityDraftUpdate
) -> CapacityStudyRecord:
    if record.status != "draft":
        raise CapacityError("only a draft capacity study can be edited", status_code=409)
    if record.revision != request.expected_revision:
        raise CapacityError(
            "capacity draft changed in another session; reload before saving",
            status_code=409,
            code="capacity_revision_conflict",
        )
    current = CapacityDraft.model_validate(record.draft_json)
    removed_blockers = sorted(set(current.build.unresolved) - set(request.draft.build.unresolved))
    if removed_blockers:
        raise CapacityError(
            "build blockers can only be resolved by scripted build validation",
            status_code=409,
            code="capacity_build_blocker_bypass",
        )
    safety = _build_plan_constraints(request.draft.build)
    if request.draft.build.approved and (request.draft.build.unresolved or safety):
        raise CapacityError(
            "an unresolved or unsafe build plan cannot be approved",
            status_code=409,
            code="capacity_build_approval_blocked",
        )
    return _commit_revisioned_draft(
        session,
        record,
        request.expected_revision,
        {
            "draft_json": request.draft.model_dump(mode="json", by_alias=True),
            "current_step": request.current_step,
            "revision": request.expected_revision + 1,
            "preflight_json": {},
            "updated_at": utc_now(),
        },
    )


def _valid_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def draft_constraints(draft: CapacityDraft) -> list[dict[str, Any]]:
    constraints: list[dict[str, Any]] = []

    def add(code: str, label: str, passed: bool, detail: str, *, group: str) -> None:
        constraints.append(
            {
                "code": code,
                "group": group,
                "label": label,
                "status": "pass" if passed else "fail",
                "blocking": True,
                "detail": detail,
            }
        )

    safety = _build_plan_constraints(draft.build)
    add(
        "build.approved",
        "构建方案已审核",
        draft.build.approved and not draft.build.unresolved and not safety,
        (
            "已审核"
            if draft.build.approved and not draft.build.unresolved and not safety
            else "必须解决所有构建项并明确审核"
        ),
        group="构建",
    )
    add(
        "scenario.steps",
        "业务链路非空",
        bool(draft.scenario.steps),
        f"{len(draft.scenario.steps)} 个步骤",
        group="场景",
    )
    has_write = any(
        step.method not in {"GET", "HEAD", "OPTIONS"} or step.side_effect not in {"none", "read"}
        for step in draft.scenario.steps
    )
    reset_ready = not has_write or (
        draft.scenario.reset_strategy == "compose-recreate"
        or (
            draft.scenario.reset_strategy == "custom" and bool(draft.scenario.reset_command.strip())
        )
    )
    add(
        "scenario.reset",
        "写接口具备重置方案",
        reset_ready,
        "无需重置或已配置" if reset_ready else "写接口必须选择重建环境或提供重置命令",
        group="场景",
    )
    add(
        "targets.sut",
        "至少一台被测服务器",
        bool(draft.targets.sut_ids),
        f"{len(draft.targets.sut_ids)} 台",
        group="服务器",
    )
    generator_by_network = {
        "internal": draft.targets.internal_load_generator_id,
        "external": draft.targets.external_load_generator_id,
    }
    generators = [generator_by_network[network] for network in draft.targets.enabled_networks]
    generator_ready = all(generators) and not set(generators) & set(draft.targets.sut_ids)
    add(
        "targets.generators",
        "施压机与被测机分离",
        generator_ready,
        (
            f"{', '.join(draft.targets.enabled_networks)} 施压机已选择"
            if generator_ready
            else "必须为每个启用网络选择非被测机作为施压机"
        ),
        group="服务器",
    )
    urls_by_network = {
        "internal": draft.targets.internal_base_urls,
        "external": draft.targets.external_base_urls,
    }
    urls_ready = (
        all(
            _valid_http_url(urls_by_network[network].get(target_id, ""))
            for network in draft.targets.enabled_networks
            for target_id in draft.targets.sut_ids
        )
        if draft.targets.sut_ids
        else False
    )
    add(
        "targets.urls",
        "启用网络地址完整",
        urls_ready,
        "地址完整且格式有效" if urls_ready else "每台被测机都必须填写启用网络的 HTTP(S) 地址",
        group="服务器",
    )
    # Frontier calibration starts at 0.5x reference load. Validating only the
    # reference point admits a contract whose very first attempt can never
    # satisfy the per-attempt tail-evidence requirement.
    initial_load_fraction = 0.5
    initial_expected_samples = (
        draft.budget.reference_rps
        * initial_load_fraction
        * draft.budget.measurement_seconds
    )
    enough_samples = initial_expected_samples >= draft.slo.minimum_samples
    add(
        "slo.samples",
        "初始负载具备尾延迟样本",
        enough_samples,
        (
            f"0.5×参考负载预计 {initial_expected_samples:g} 个样本"
            if enough_samples
            else (
                f"0.5×参考负载仅预计 {initial_expected_samples:g} 个样本；"
                "提高参考 RPS、测量时间或降低最小样本数"
            )
        ),
        group="SLO",
    )
    return constraints


def _ssh_docker_check(target_id: str, settings: Settings) -> tuple[bool, str]:
    if target_id == "local":
        return False, "控制平面本机不能作为被测服务器"
    try:
        request = EncryptedSshCredentialStore(settings).load(target_id)
        client = open_ssh_client(request)
        try:
            _stdin, stdout, stderr = client.exec_command(
                "docker compose version --short", timeout=request.timeout_seconds
            )
            output = stdout.read(4096).decode("utf-8", errors="replace").strip()
            error = stderr.read(4096).decode("utf-8", errors="replace").strip()
            status = stdout.channel.recv_exit_status()
            if status != 0:
                return False, f"Docker Compose 不可用：{error[-300:] or status}"
            return True, f"Docker Compose {output or 'ready'}"
        finally:
            client.close()
    except Exception as error:
        return False, f"SSH/Docker 检查失败：{str(error)[:300]}"


def preflight_capacity_study(
    session: Session,
    record: CapacityStudyRecord,
    settings: Settings,
    *,
    probe_ssh: bool = True,
) -> dict[str, Any]:
    if record.status != "draft":
        raise CapacityError("only a draft capacity study can be preflighted", status_code=409)
    draft = CapacityDraft.model_validate(record.draft_json)
    checks: list[dict[str, Any]] = []
    failed_suts: list[str] = []
    for target_id in draft.targets.sut_ids:
        target = session.get(TargetRecord, target_id)
        if target is None or target.lifecycle_status != "active":
            passed, detail = False, "目标不存在或已归档"
        elif probe_ssh:
            passed, detail = _ssh_docker_check(target_id, settings)
        else:
            try:
                remembered = target_id in EncryptedSshCredentialStore(settings).target_ids()
            except RemoteCredentialError:
                remembered = False
            passed = remembered
            detail = "已保存 SSH 凭据" if remembered else "缺少已验证的 SSH 凭据"
        checks.append({"scope": "sut", "targetId": target_id, "passed": passed, "detail": detail})
        if not passed:
            failed_suts.append(target_id)
    generator_failures: list[str] = []
    generator_by_network = {
        "internal": draft.targets.internal_load_generator_id,
        "external": draft.targets.external_load_generator_id,
    }
    for network in draft.targets.enabled_networks:
        target_id = generator_by_network[network]
        target = session.get(TargetRecord, target_id) if target_id else None
        passed = bool(
            target
            and target.lifecycle_status == "active"
            and target.runnable
            and target_id not in draft.targets.sut_ids
        )
        detail = "Worker 在线且与被测机分离" if passed else "需要在线 Worker，且不能与被测机相同"
        checks.append(
            {
                "scope": "load-generator",
                "network": network,
                "targetId": target_id,
                "passed": passed,
                "detail": detail,
            }
        )
        if not passed:
            generator_failures.append(network)
    result = {
        "status": "pass" if not failed_suts and not generator_failures else "fail",
        "draftRevision": record.revision,
        "checkedAt": utc_now().isoformat(),
        "checks": checks,
        "failedSutIds": failed_suts,
        "generatorFailures": generator_failures,
    }
    record.preflight_json = result
    record.updated_at = utc_now()
    return result


def start_capacity_study(
    session: Session,
    record: CapacityStudyRecord,
    request: CapacityStartRequest,
    settings: Settings,
) -> CapacityStudyRecord:
    if record.status != "draft":
        raise CapacityError("capacity study has already been started", status_code=409)
    if record.revision != request.expected_revision:
        raise CapacityError("capacity draft revision changed; run preflight again", status_code=409)
    discovery = session.get(SourceDiscoveryRecord, record.discovery_id)
    if discovery is None:
        raise CapacityError("capacity study source discovery is unavailable", status_code=409)
    _retained_source_archive(discovery, settings)
    constraints = draft_constraints(CapacityDraft.model_validate(record.draft_json))
    blocking = [item for item in constraints if item["status"] == "fail"]
    if blocking:
        raise CapacityError("capacity draft has blocking constraints", constraints=constraints)
    preflight = record.preflight_json or {}
    if preflight.get("draftRevision") != record.revision:
        raise CapacityError("preflight is missing or stale", code="capacity_preflight_stale")
    if preflight.get("generatorFailures"):
        raise CapacityError("load generator preflight must pass before starting")
    failed = set(preflight.get("failedSutIds") or [])
    excluded = set(request.excluded_target_ids)
    if failed != excluded:
        raise CapacityError("excluded targets must exactly match failed SUT preflight targets")
    if failed and not request.acknowledge_partial:
        raise CapacityError("partial execution requires explicit acknowledgement")
    draft = CapacityDraft.model_validate(record.draft_json)
    active_targets = [target_id for target_id in draft.targets.sut_ids if target_id not in excluded]
    if not active_targets:
        raise CapacityError("at least one preflight-passing SUT is required")
    started_at = utc_now()
    previous_execution = dict(record.execution_json or {})
    execution = {
        "phases": [{"id": "queued", "status": "running", "at": started_at.isoformat()}],
        "buildValidations": list(previous_execution.get("buildValidations") or []),
        "selectedTargetIds": draft.targets.sut_ids,
        "excludedTargetIds": sorted(excluded),
        "activeTargetIds": active_targets,
        "acknowledgedPartial": bool(failed),
        "budget": draft.budget.model_dump(mode="json", by_alias=True),
        "costControl": {
            "currency": "CNY",
            "limit": draft.budget.cost_cap,
            "scope": "incremental capacity-run charges",
            "pricingStatus": "not-applicable-existing-resources",
            "estimatedIncrementalAmount": 0.0,
            "detail": (
                "本次容量运行只使用已登记服务器，不创建云资源；既有服务器租金不计入增量费用。"
            ),
        },
        "runs": [],
        "currentNetwork": draft.targets.enabled_networks[0],
    }
    return _commit_revisioned_draft(
        session,
        record,
        request.expected_revision,
        {
            "status": "queued",
            "revision": request.expected_revision + 1,
            "started_at": started_at,
            "updated_at": started_at,
            "execution_json": execution,
        },
    )


def capacity_view(
    session: Session,
    record: CapacityStudyRecord,
    settings: Settings | None = None,
) -> dict[str, Any]:
    discovery = session.get(SourceDiscoveryRecord, record.discovery_id)
    draft = CapacityDraft.model_validate(record.draft_json)
    constraints = draft_constraints(draft)
    archive = {"status": "unavailable", "expiresAt": None}
    if discovery is not None:
        now = utc_now()
        retained = discovery.archive_retained_until
        comparable_now = now.replace(tzinfo=None) if retained and retained.tzinfo is None else now
        exists = settings is None or EncryptedSourceArchiveStore(settings).available(discovery.id)
        if discovery.archive_deleted_at is not None:
            status = "deleted"
        elif retained is not None and retained > comparable_now and exists:
            status = "retained"
        elif retained is not None:
            status = "expired"
        else:
            status = "unavailable"
        archive = {
            "status": status,
            "expiresAt": retained.isoformat() if retained else None,
            "keyProtection": (
                EncryptedSourceArchiveStore(settings).key_protection(discovery.id)
                if settings is not None and status == "retained"
                else None
            ),
        }
    execution = dict(record.execution_json)
    execution["liveMatrix"] = _capacity_live_matrix(session, record)
    return {
        "id": record.id,
        "discoveryId": record.discovery_id,
        "discoveryName": discovery.archive_name if discovery else None,
        "sourceDigest": discovery.source_digest if discovery else None,
        "sourceArchive": archive,
        "name": record.name,
        "status": record.status,
        "revision": record.revision,
        "currentStep": record.current_step,
        "draft": draft.model_dump(mode="json", by_alias=True),
        "constraints": constraints,
        "readyToPreflight": not any(item["status"] == "fail" for item in constraints),
        "preflight": record.preflight_json,
        "execution": execution,
        "report": record.report_json,
        "error": {"code": record.error_code, "message": record.error_message}
        if record.error_code
        else None,
        "createdAt": record.created_at.isoformat(),
        "updatedAt": record.updated_at.isoformat(),
        "startedAt": record.started_at.isoformat() if record.started_at else None,
        "completedAt": record.completed_at.isoformat() if record.completed_at else None,
    }


def _capacity_live_matrix(session: Session, record: CapacityStudyRecord) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run in record.execution_json.get("runs") or []:
        experiment_id = str(run.get("experimentId") or "")
        points = list(
            session.scalars(
                select(SelectionLoadPointRecord)
                .where(SelectionLoadPointRecord.experiment_id == experiment_id)
                .order_by(SelectionLoadPointRecord.sequence)
            )
        )
        current = next(
            (point for point in points if point.status not in {"complete", "unresolved"}),
            points[-1] if points else None,
        )
        analysis = (points[-1].analysis_json or {}) if points else {}
        frontiers = analysis.get("target_frontiers") or {}
        for target_id in record.execution_json.get("activeTargetIds") or []:
            frontier = frontiers.get(f"business-iteration:{target_id}") or {}
            rows.append(
                {
                    "network": run.get("network"),
                    "targetId": target_id,
                    "experimentId": experiment_id,
                    "currentLoad": float(current.offered_load) if current is not None else None,
                    "pointStatus": current.status if current is not None else "queued",
                    "sloStatus": frontier.get("status") or "collecting-evidence",
                    "confirmedPass": frontier.get("confirmed_pass"),
                    "confirmedFail": frontier.get("confirmed_fail"),
                }
            )
    return rows


def _metric_declaration(
    unit: str,
    direction: str,
    *,
    role: str,
    label: str,
    minimum_samples: int = 1,
) -> dict[str, Any]:
    return {
        "unit": unit,
        "direction": direction,
        "kind": "aggregate",
        "required": True,
        "minimumSamples": minimum_samples,
        "presentation": {
            "userLabel": label,
            "roles": [role],
            "defaultVisibility": "summary" if role == "primary_outcome" else "detail",
            "displayFormat": "duration" if unit == "ms" else "number",
            "displayPrecision": 4 if unit == "ratio" else 2,
        },
    }


def _scenario_contract(record: CapacityStudyRecord) -> ScenarioBenchmarkSpec:
    draft = CapacityDraft.model_validate(record.draft_json)
    return ScenarioBenchmarkSpec(
        id=f"http.capacity.{record.id.removeprefix('capacity_')[:16]}",
        name=record.name,
        decision_question="各候选服务器在业务正确性与尾延迟 SLO 内的容量区间是多少？",
        user_value="用可复核的业务迭代而不是单接口请求数支持服务器容量决策。",
        workload_class="http-business-api",
        topology="client-server",
        roles=[
            ScenarioRoleSpec(
                id="target",
                kind="target",
                included_in_score=True,
                description="运行待测业务应用的候选服务器",
            ),
            ScenarioRoleSpec(
                id="load-generator",
                kind="load-generator",
                included_in_score=False,
                description="与被测机分离的开放到达率 HTTP 客户端",
            ),
        ],
        primary_metric="committed_tps",
        slo_gates=[
            GateSpec(
                id="p99-latency",
                kind=GateKind.SLO,
                scope=GateScope.BLOCK,
                metric="latency_p99_ms",
                operator=Operator.LTE,
                threshold=draft.slo.p99_ms,
                hard=True,
            ),
            GateSpec(
                id="p999-latency",
                kind=GateKind.SLO,
                scope=GateScope.BLOCK,
                metric="latency_p999_ms",
                operator=Operator.LTE,
                threshold=draft.slo.p999_ms,
                hard=True,
            ),
            GateSpec(
                id="success-rate",
                kind=GateKind.CORRECTNESS,
                scope=GateScope.BLOCK,
                metric="success_rate",
                operator=Operator.GTE,
                threshold=draft.slo.minimum_success_rate,
                hard=True,
            ),
        ],
        goodput=GoodputPolicy(
            metric="committed_tps",
            unit="iterations/second",
            committed_outcome="successful-business-iteration",
            maximum_error_ratio=draft.slo.maximum_error_rate,
            maximum_abort_ratio=1.0,
            maximum_timeout_ratio=draft.slo.maximum_timeout_rate,
        ),
        tail_evidence=TailEvidenceSpec(
            metric="business_iteration_latency",
            unit="ms",
            minimum_samples=draft.slo.minimum_samples,
            required_statistics=["p50", "p95", "p99", "p99.9", "maximum", "timeout"],
            histogram_format="upstream-summary",
            timeout_accounting="separate",
        ),
        load_search=LoadSearchSpec(
            offered_load_metric="offered_tps",
            unit="iterations/second",
            calibration_repeats=3,
            common_load_fractions=[0.5, 0.75, 1.0],
            initial_repeats=3,
            boundary_repeats=5,
            required_passes=4,
            resolution_ratio=0.025,
            minimum_effect_ratio=0.05,
            maximum_adaptive_points=5,
            expansion_factor=1.25,
        ),
    )


def _capacity_manifest(record: CapacityStudyRecord, settings: Settings) -> dict[str, Any]:
    scenario = _scenario_contract(record)
    digest = canonical_digest({"dependencies": []})
    metrics = {
        "offered_tps": _metric_declaration(
            "iterations/second", "none", role="context", label="施加业务迭代"
        ),
        "attempted_tps": _metric_declaration(
            "iterations/second", "none", role="context", label="实际发起迭代"
        ),
        "committed_tps": _metric_declaration(
            "iterations/second", "maximize", role="primary_outcome", label="成功业务容量"
        ),
        "offered_requests": _metric_declaration(
            "iterations", "none", role="diagnostic", label="计划迭代数"
        ),
        "started_requests": _metric_declaration(
            "iterations", "none", role="diagnostic", label="开始迭代数"
        ),
        "completed_requests": _metric_declaration(
            "iterations", "none", role="diagnostic", label="完成迭代数"
        ),
        "success_rate": _metric_declaration(
            "ratio", "maximize", role="hard_gate", label="业务成功率"
        ),
        "error_ratio": _metric_declaration("ratio", "minimize", role="hard_gate", label="错误率"),
        "abort_ratio": _metric_declaration("ratio", "minimize", role="diagnostic", label="中止率"),
        "timeout_ratio": _metric_declaration("ratio", "minimize", role="hard_gate", label="超时率"),
        "offered_load_achieved_ratio": _metric_declaration(
            "ratio", "maximize", role="diagnostic", label="负载达成率"
        ),
        "rate_limiter_lag_ratio": _metric_declaration(
            "ratio", "minimize", role="diagnostic", label="限流器滞后率"
        ),
        "client_headroom_ratio": _metric_declaration(
            "ratio", "maximize", role="diagnostic", label="施压机余量"
        ),
    }
    for name, label in (
        ("latency_p50_ms", "P50 延迟"),
        ("latency_p95_ms", "P95 延迟"),
        ("latency_p99_ms", "P99 延迟"),
        ("latency_p999_ms", "P99.9 延迟"),
        ("latency_max_ms", "最大延迟"),
    ):
        metrics[name] = _metric_declaration(
            "ms",
            "minimize",
            role="hard_gate" if name in {"latency_p99_ms", "latency_p999_ms"} else "diagnostic",
            label=label,
            minimum_samples=scenario.tail_evidence.minimum_samples
            if scenario.tail_evidence
            else 100,
        )
    benchmark_id = f"looper.http.capacity.{record.id.removeprefix('capacity_')[:16]}"
    return {
        "apiVersion": "looper.dev/v1alpha1",
        "kind": "Benchmark",
        "metadata": {
            "id": benchmark_id,
            "name": f"HTTP capacity · {record.name}"[:120],
            "version": f"r{record.revision}",
            "description": "Looper-generated real HTTP business-iteration capacity workload.",
            "license": "INTERNAL",
            "source": {
                "url": f"looper://capacity/{record.id}",
                "digest": canonical_digest({"study": record.id, "revision": record.revision}),
            },
        },
        "spec": {
            "trust": "trusted",
            "capabilities": ["python", "local-process"],
            "parameters": {},
            "workloads": [{"id": "business-iteration", "name": "业务接口链", "weight": 1}],
            "scenario": scenario.model_dump(mode="json"),
            "infrastructure": {
                "orchestration": "looper",
                "primaryNodeGroup": "target",
                "nodeGroups": [
                    {
                        "id": "target",
                        "role": "target",
                        "count": {"minimum": 1, "default": 1, "maximum": 1},
                        "includedInScore": True,
                        "requirements": {"osFamilies": ["linux"]},
                        "placement": {"separateFrom": ["load-generator"], "dedicated": True},
                    },
                    {
                        "id": "load-generator",
                        "role": "load-generator",
                        "count": {"minimum": 1, "default": 1, "maximum": 1},
                        "includedInScore": False,
                        "requirements": {
                            "osFamilies": ["linux"],
                            "capabilities": ["python", "local-process"],
                        },
                        "placement": {"separateFrom": ["target"], "dedicated": True},
                    },
                ],
            },
            "adapter": {
                "protocol": "looper-adapter/v1",
                "executionModel": "service-stack",
                "primaryMetric": "committed_tps",
                "requiredChecks": [
                    "business-response-correctness",
                    "load-generator-validity",
                ],
                "inputs": [
                    {
                        "id": "capacity-config",
                        "kind": "config",
                        "required": True,
                        "mediaType": "application/json",
                        "digestRequired": True,
                        "description": (
                            "Immutable business flow, endpoints, and measurement window."
                        ),
                    }
                ],
                "canonicalOutputs": {"metrics": "metrics.jsonl", "result": "result.json"},
            },
            "runtime": {
                "type": "local-process",
                "workingDirectory": ".",
                "dependencyLockDigest": digest,
                "dependencies": [],
                "provisioning": {
                    "mode": "managed",
                    "hostCapabilities": ["python", "local-process"],
                    "provides": ["http-capacity-client"],
                    "cacheKey": digest,
                    "requiresNetwork": False,
                    "privilege": "none",
                },
                "commands": {
                    "prepare": {
                        "argv": [
                            "{python}",
                            "{benchmarkRoot}/prepare.py",
                            "--benchmark-root",
                            "{benchmarkRoot}",
                            "--cache",
                            "{cache}",
                        ],
                        "timeoutSeconds": 30,
                        "allowedExitCodes": [0],
                    },
                    "run": {
                        "argv": [
                            "{python}",
                            "{benchmarkRoot}/runner.py",
                            "--envelope",
                            "{envelope}",
                            "--output",
                            "{output}",
                        ],
                        "environment": {
                            "LOOPER_DATA_DIR": str(settings.data_dir.resolve()),
                        },
                        "timeoutSeconds": 86400,
                        "allowedExitCodes": [0],
                    },
                    "normalize": {
                        "argv": [
                            "{python}",
                            "{benchmarkRoot}/normalizer.py",
                            "--envelope",
                            "{envelope}",
                            "--output",
                            "{output}",
                        ],
                        "timeoutSeconds": 60,
                        "allowedExitCodes": [0],
                    },
                },
            },
            "metrics": metrics,
            "outputs": {
                "maxBytes": 16777216,
                "maxMetricLines": 100,
                "artifacts": [
                    {
                        "path": "capacity-native.json",
                        "role": "raw-result",
                        "mediaType": "application/json",
                        "required": True,
                    },
                    {
                        "path": "result.json",
                        "role": "result",
                        "mediaType": "application/json",
                        "required": True,
                    },
                    {
                        "path": "benchmark.log",
                        "role": "log",
                        "mediaType": "text/plain",
                        "required": True,
                    },
                ],
            },
            "audit": {
                "minimumRepeats": 5,
                "referencePolicy": "not-applicable",
                "environmentAxes": ["machine", "network"],
                "requiredEvidence": ["system-fingerprint", "raw-result", "log"],
            },
        },
    }


def ensure_capacity_benchmark(
    session: Session, record: CapacityStudyRecord, settings: Settings
) -> BenchmarkRecord:
    manifest_document = _capacity_manifest(record, settings)
    metadata = manifest_document["metadata"]
    key = f"{metadata['id']}@{metadata['version']}"
    existing = session.get(BenchmarkRecord, key)
    if existing is not None:
        return existing
    package_root = (settings.capacity_package_dir / record.id / f"r{record.revision}").resolve()
    package_root.mkdir(parents=True, exist_ok=True)
    template_root = Path(__file__).resolve().parents[3] / "benchmarks" / "http-capacity"
    for name in ("prepare.py", "runner.py", "normalizer.py", "dependency-lock.json"):
        shutil.copy2(template_root / name, package_root / name)
    manifest_path = package_root / "benchmark.json"
    manifest_path.write_text(
        json.dumps(manifest_document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest, manifest_digest = load_and_validate_manifest(manifest_path)
    benchmark = BenchmarkRecord(
        key=key,
        benchmark_id=metadata["id"],
        version=metadata["version"],
        name=metadata["name"],
        description=metadata["description"],
        license=metadata["license"],
        manifest_digest=manifest_digest,
        manifest_json=manifest,
        manifest_path=str(manifest_path),
        package_digest=None,
        trusted=True,
        installed_at=utc_now(),
    )
    session.add(benchmark)
    session.flush()
    return benchmark


def _network_experiment_request(
    session: Session,
    record: CapacityStudyRecord,
    benchmark: BenchmarkRecord,
    network: Literal["internal", "external"],
) -> ExperimentCreate:
    draft = CapacityDraft.model_validate(record.draft_json)
    execution = record.execution_json
    active_targets = list(execution["activeTargetIds"])
    endpoints = (
        draft.targets.internal_base_urls
        if network == "internal"
        else draft.targets.external_base_urls
    )
    load_generator = (
        draft.targets.internal_load_generator_id
        if network == "internal"
        else draft.targets.external_load_generator_id
    )
    scenario = _scenario_contract(record)
    metadata = {
        "network": network,
        "scenario": draft.scenario.model_dump(mode="json", by_alias=True),
        "endpoints": {target_id: endpoints[target_id] for target_id in active_targets},
        "measurementSeconds": draft.budget.measurement_seconds,
        "servicePort": draft.build.service_port,
        "requestTimeoutSeconds": max(1, min(30, draft.slo.p999_ms / 1000 * 2)),
        "sourceDigest": session.get(SourceDiscoveryRecord, record.discovery_id).source_digest,
    }
    input_digest = canonical_digest(metadata)
    bindings = []
    for target_id in active_targets:
        target = session.get(TargetRecord, target_id)
        if target is None:
            raise CapacityError(f"target {target_id!r} disappeared before execution")
        variant = str(
            (target.inventory_json or {}).get("instance_type")
            or (target.inventory_json or {}).get("instanceType")
            or target.id
        )
        bindings.append(
            TargetBindingSpec(
                target_id=target.id,
                variant_id=variant,
                label=target.name,
                placement_pair_id=f"{record.id}:{network}",
            )
        )
    per_network_attempts = max(
        1, draft.budget.max_attempts // len(draft.targets.enabled_networks)
    )
    spec = ExperimentSpec(
        mode=ExperimentMode.SELECTION,
        benchmark_id=benchmark.benchmark_id,
        benchmark_version=benchmark.version,
        target_ids=active_targets,
        workload_ids=["business-iteration"],
        input_bindings={
            "capacity-config": BenchmarkInputBinding(
                kind="config",
                reference=f"looper://capacity/{record.id}/{network}",
                digest=input_digest,
                metadata=metadata,
            )
        },
        objectives=[
            ObjectiveSpec(
                metric="committed_tps",
                unit="iterations/second",
                direction=Direction.MAXIMIZE,
                aggregation=Aggregation.MEDIAN,
                comparison=Comparison.RELATIVE,
                minimum_samples=5,
            )
        ],
        design=ExperimentalDesign(
            warmup_runs=0,
            min_repeats=5,
            max_repeats=5,
            max_retries=1,
            baseline_every_n=1,
            confidence_level=draft.slo.confidence_level,
            bootstrap_resamples=2000,
            tail_min_samples=draft.slo.minimum_samples,
            random_seed=20260824,
        ),
        budget=BudgetSpec(
            max_candidates=len(active_targets),
            max_attempts=per_network_attempts,
            wall_time_seconds=max(60, draft.budget.max_seconds // 2),
        ),
        scenario=scenario,
        selection=SelectionDesign(
            target_bindings=bindings,
            reference_offered_load=draft.budget.reference_rps,
            load_generator_target_id=load_generator,
            order_scheme="balanced-random",
            inference_unit="time_block",
            minimum_placement_pairs=1,
            random_seed=20260824,
        ),
    )
    return ExperimentCreate(
        name=f"{record.name} · {'内网' if network == 'internal' else '公网'}",
        description=f"{network} HTTP business-iteration SLO capacity frontier",
        spec=spec,
    )


def create_capacity_experiment(
    session: Session,
    record: CapacityStudyRecord,
    settings: Settings,
    network: Literal["internal", "external"],
) -> ExperimentRecord:
    from looper_api.scheduler import create_experiment, start_experiment

    benchmark = ensure_capacity_benchmark(session, record, settings)
    experiment = create_experiment(
        session, _network_experiment_request(session, record, benchmark, network)
    )
    session.flush()
    start_experiment(session, experiment)
    execution = dict(record.execution_json)
    runs = list(execution.get("runs") or [])
    draft = CapacityDraft.model_validate(record.draft_json)
    runs.append(
        {
            "network": network,
            "experimentId": experiment.id,
            "loadGeneratorTargetId": (
                draft.targets.internal_load_generator_id
                if network == "internal"
                else draft.targets.external_load_generator_id
            ),
            "status": experiment.status,
            "startedAt": utc_now().isoformat(),
        }
    )
    execution["runs"] = runs
    execution["currentNetwork"] = network
    record.execution_json = execution
    record.status = "running"
    record.updated_at = utc_now()
    return experiment


def _remote_run(client: Any, command: str, *, timeout: int = 900) -> str:
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    output = stdout.read(1024 * 1024).decode("utf-8", errors="replace")
    error = stderr.read(256 * 1024).decode("utf-8", errors="replace")
    status = stdout.channel.recv_exit_status()
    if status != 0:
        raise CapacityError(
            f"remote capacity command failed: {' '.join(error.split())[-1000:] or status}",
            code="capacity_remote_command_failed",
        )
    return output


def _remote_paths(client: Any, study_id: str) -> tuple[PurePosixPath, PurePosixPath]:
    home = _remote_run(client, "printf '%s' \"$HOME\"", timeout=30).strip()
    home_path = PurePosixPath(home)
    if not home_path.is_absolute() or ".." in home_path.parts:
        raise CapacityError("remote account returned an invalid home directory")
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "-", study_id)
    root = home_path / ".looper-capacity" / safe_id
    return root, root / "source"


def _compose_project(record: CapacityStudyRecord, target_id: str) -> str:
    return re.sub(
        r"[^a-z0-9-]",
        "-",
        f"looper-{record.id[-12:]}-{target_id[-12:]}".casefold(),
    ).strip("-")


def deploy_capacity_target(
    record: CapacityStudyRecord,
    target_id: str,
    archive: bytes,
    settings: Settings,
) -> dict[str, Any]:
    draft = CapacityDraft.model_validate(record.draft_json)
    request = EncryptedSshCredentialStore(settings).load(target_id)
    client = open_ssh_client(request)
    try:
        root, source = _remote_paths(client, record.id)
        source_root = _safe_relative_path(draft.build.source_root, label="source root")
        project_source = source if source_root.as_posix() == "." else source / source_root
        build_context, dockerfile = _compose_build_target(draft.build)
        generated_dockerfile = project_source / build_context / dockerfile
        root_text = str(root)
        if "/.looper-capacity/" not in root_text:
            raise CapacityError("remote capacity root failed safety validation")
        _remote_run(
            client,
            f"rm -rf {shlex.quote(root_text)} && mkdir -p {shlex.quote(root_text)}",
            timeout=60,
        )
        sftp = client.open_sftp()
        try:
            with sftp.file(str(root / "source.zip"), "wb") as stream:
                stream.write(archive)
        finally:
            sftp.close()
        _remote_run(
            client,
            f"mkdir -p {shlex.quote(str(source))} && python3 -m zipfile -e "
            f"{shlex.quote(str(root / 'source.zip'))} {shlex.quote(str(source))}",
            timeout=300,
        )
        _remote_run(
            client,
            f"test -d {shlex.quote(str(project_source))} && "
            f"mkdir -p {shlex.quote(str(generated_dockerfile.parent))}",
            timeout=60,
        )
        sftp = client.open_sftp()
        try:
            with sftp.file(str(generated_dockerfile), "w") as stream:
                stream.write(draft.build.dockerfile)
            with sftp.file(str(project_source / "compose.capacity.yaml"), "w") as stream:
                stream.write(draft.build.compose)
        finally:
            sftp.close()
        if draft.build.ordered_migrations:
            migration_root = project_source / ".looper-capacity-migrations"
            _remote_run(
                client,
                f"mkdir -p {shlex.quote(str(migration_root))}",
                timeout=60,
            )
            for index, relative in enumerate(draft.build.ordered_migrations, 1):
                migration = _safe_relative_path(relative, label="ordered migration")
                source_file = project_source / migration
                destination = migration_root / f"{index:04d}_{migration.name}"
                _remote_run(
                    client,
                    f"test -f {shlex.quote(str(source_file))} && cp "
                    f"{shlex.quote(str(source_file))} {shlex.quote(str(destination))}",
                    timeout=60,
                )
        project = _compose_project(record, target_id)
        compose_prefix = (
            f"cd {shlex.quote(str(project_source))} && docker compose "
            f"-p {shlex.quote(project)} -f compose.capacity.yaml"
        )
        try:
            _remote_run(client, f"{compose_prefix} config -q", timeout=60)
            _remote_run(client, f"{compose_prefix} build", timeout=1800)
            _remote_run(
                client,
                f"{compose_prefix} up -d --wait --wait-timeout 120 --remove-orphans",
                timeout=180,
            )
            health_url = f"http://127.0.0.1:{draft.build.service_port}{draft.build.health_path}"
            health_script = (
                "import sys,time,urllib.request\n"
                f"url={health_url!r}\n"
                "deadline=time.time()+120\n"
                "while time.time()<deadline:\n"
                " try:\n"
                "  response=urllib.request.urlopen(url,timeout=3)\n"
                "  sys.exit(0 if response.status<500 else 1)\n"
                " except Exception:\n"
                "  time.sleep(2)\n"
                "sys.exit(2)\n"
            )
            encoded = json.dumps(health_script)
            _remote_run(
                client,
                f"python3 -c {shlex.quote('exec(' + encoded + ')')}",
                timeout=150,
            )
        except Exception as error:
            try:
                logs = _remote_run(
                    client,
                    f"{compose_prefix} logs --no-color --tail 200",
                    timeout=60,
                )
            except Exception as log_error:
                logs = f"compose logs unavailable: {log_error}"
            raise CapacityError(
                f"scripted build validation failed: {error}; logs: {logs[-6000:]}",
                code="capacity_build_validation_failed",
            ) from error
        return {
            "targetId": target_id,
            "remoteRoot": root_text,
            "project": project,
            "status": "healthy",
            "deployedAt": utc_now().isoformat(),
        }
    finally:
        client.close()


def cleanup_capacity_target(
    record: CapacityStudyRecord, target_id: str, settings: Settings
) -> dict[str, Any]:
    request = EncryptedSshCredentialStore(settings).load(target_id)
    client = open_ssh_client(request)
    try:
        root, source = _remote_paths(client, record.id)
        draft = CapacityDraft.model_validate(record.draft_json)
        source_root = _safe_relative_path(draft.build.source_root, label="source root")
        project_source = source if source_root.as_posix() == "." else source / source_root
        root_text = str(root)
        if "/.looper-capacity/" not in root_text:
            raise CapacityError("remote capacity root failed safety validation")
        project = _compose_project(record, target_id)
        _remote_run(
            client,
            f"if test -d {shlex.quote(str(project_source))}; then "
            f"cd {shlex.quote(str(project_source))} && "
            f"docker compose -p {shlex.quote(project)} -f compose.capacity.yaml down "
            "--volumes --remove-orphans; fi",
            timeout=600,
        )
        _remote_run(client, f"rm -rf {shlex.quote(root_text)}", timeout=60)
        return {"targetId": target_id, "status": "clean", "cleanedAt": utc_now().isoformat()}
    finally:
        client.close()


def reset_capacity_targets(record: CapacityStudyRecord, settings: Settings) -> None:
    draft = CapacityDraft.model_validate(record.draft_json)
    for target_id in record.execution_json.get("activeTargetIds") or []:
        request = EncryptedSshCredentialStore(settings).load(target_id)
        client = open_ssh_client(request)
        try:
            _root, source = _remote_paths(client, record.id)
            source_root = _safe_relative_path(draft.build.source_root, label="source root")
            project_source = source if source_root.as_posix() == "." else source / source_root
            project = _compose_project(record, target_id)
            if draft.scenario.reset_strategy == "custom":
                command = draft.scenario.reset_command
            else:
                command = (
                    f"docker compose -p {shlex.quote(project)} -f compose.capacity.yaml "
                    "down --volumes --remove-orphans && "
                    f"docker compose -p {shlex.quote(project)} -f compose.capacity.yaml up -d"
                )
            _remote_run(
                client,
                f"cd {shlex.quote(str(project_source))} && {command}",
                timeout=900,
            )
        finally:
            client.close()


_capacity_jobs: set[str] = set()
_capacity_jobs_lock = threading.Lock()


def _phase(record: CapacityStudyRecord, phase_id: str, status: str, detail: str) -> None:
    execution = dict(record.execution_json)
    phases = list(execution.get("phases") or [])
    phases.append({"id": phase_id, "status": status, "detail": detail, "at": utc_now().isoformat()})
    execution["phases"] = phases
    record.execution_json = execution
    record.updated_at = utc_now()


def _cleanup_all(record: CapacityStudyRecord, settings: Settings) -> list[dict[str, Any]]:
    results = []
    for target_id in record.execution_json.get("activeTargetIds") or []:
        try:
            results.append(cleanup_capacity_target(record, target_id, settings))
        except Exception as error:
            results.append({"targetId": target_id, "status": "failed", "detail": str(error)[:500]})
    return results


def _prepare_capacity_job(study_id: str, settings: Settings) -> None:
    from looper_api.database import SessionLocal

    with SessionLocal() as session:
        record = session.get(CapacityStudyRecord, study_id)
        if record is None or record.status != "queued":
            return
        record.status = "deploying"
        _phase(record, "deploying", "running", "正在部署隔离 Compose 环境")
        session.commit()
    deployed: list[dict[str, Any]] = []
    try:
        with SessionLocal() as session:
            record = session.get(CapacityStudyRecord, study_id)
            discovery = session.get(SourceDiscoveryRecord, record.discovery_id) if record else None
            if record is None or discovery is None:
                raise CapacityError("capacity study source discovery disappeared")
            archive = _retained_source_archive(discovery, settings)
            for target_id in record.execution_json.get("activeTargetIds") or []:
                session.refresh(record)
                if record.status == "cancelling":
                    raise CapacityError(
                        "capacity study was cancelled during deployment",
                        code="capacity_cancelled",
                    )
                deployed.append(deploy_capacity_target(record, target_id, archive, settings))
            session.refresh(record)
            if record.status == "cancelling":
                raise CapacityError(
                    "capacity study was cancelled after deployment",
                    code="capacity_cancelled",
                )
            execution = dict(record.execution_json)
            execution["deployments"] = deployed
            record.execution_json = execution
            _phase(record, "deploying", "completed", "所有被测环境健康检查通过")
            draft = CapacityDraft.model_validate(record.draft_json)
            first_network = draft.targets.enabled_networks[0]
            create_capacity_experiment(session, record, settings, first_network)
            _phase(
                record,
                first_network,
                "running",
                f"正在执行 {first_network} 容量边界搜索",
            )
            session.commit()
    except Exception as error:
        with SessionLocal() as session:
            record = session.get(CapacityStudyRecord, study_id)
            if record is None:
                return
            cleanup = _cleanup_all(record, settings)
            execution = dict(record.execution_json)
            execution["deployments"] = deployed
            execution["cleanup"] = cleanup
            record.execution_json = execution
            cleanup_ok = all(item["status"] == "clean" for item in cleanup)
            cancelled = record.status == "cancelling" or (
                isinstance(error, CapacityError) and error.code == "capacity_cancelled"
            )
            build_validation_failed = (
                isinstance(error, CapacityError)
                and error.code == "capacity_build_validation_failed"
            )
            if build_validation_failed and cleanup_ok and not cancelled:
                message = str(error)[:16000]
                draft = CapacityDraft.model_validate(record.draft_json)
                draft.build.approved = False
                blocker = f"远程脚本验证失败：{message}"[:4000]
                draft.build.unresolved = [blocker]
                record.draft_json = draft.model_dump(mode="json", by_alias=True)
                execution["buildRuntimeFailure"] = blocker
                record.execution_json = execution
                record.status = "draft"
                record.current_step = 0
                record.revision += 1
                record.preflight_json = {}
                record.error_code = "capacity_build_validation_failed"
                record.error_message = message
                record.started_at = None
                record.completed_at = None
                _phase(record, "deploying", "failed", "脚本验证失败，已清理并返回构建步骤")
                session.commit()
                return
            record.status = (
                "cancelled"
                if cancelled and cleanup_ok
                else "failed"
                if cleanup_ok
                else "needs-attention"
            )
            record.error_code = (
                None
                if cancelled and cleanup_ok
                else "capacity_cleanup_failed"
                if cancelled
                else "capacity_deployment_failed"
            )
            record.error_message = None if cancelled and cleanup_ok else str(error)[:16000]
            record.completed_at = utc_now()
            _phase(
                record,
                "cleanup" if cancelled else "deploying",
                "completed" if cancelled and cleanup_ok else "failed",
                "取消后的环境清理" if cancelled else str(error)[:500],
            )
            session.commit()


def _report_capacity_study(session: Session, record: CapacityStudyRecord) -> dict[str, Any]:
    from looper_api.analysis_service import build_analysis_snapshot

    networks = []
    for run in record.execution_json.get("runs") or []:
        experiment_id = run.get("experimentId")
        experiment = session.get(ExperimentRecord, experiment_id)
        if experiment is None:
            continue
        analysis = build_analysis_snapshot(session, experiment.id, persist=True)
        networks.append(
            {
                "network": run["network"],
                "experimentId": experiment.id,
                "status": analysis.get("frontier", {}).get("status"),
                "terminationReason": analysis.get("frontier", {}).get("termination_reason"),
                "targets": analysis.get("targets") or [],
                "comparisons": analysis.get("comparisons") or [],
                "trajectory": analysis.get("frontier", {}).get("trajectory") or [],
                "evidence": analysis.get("evidence") or {},
            }
        )
    comparable_networks = 0
    unresolved = False
    overlap = False
    for network in networks:
        network_intervals: list[tuple[float, float]] = []
        for target in network["targets"]:
            frontier = next(iter((target.get("frontiers") or {}).values()), {})
            lower = frontier.get("confirmed_pass")
            upper = frontier.get("confirmed_fail")
            if lower is None or upper is None or frontier.get("status") != "resolved":
                unresolved = True
            else:
                network_intervals.append((float(lower), float(upper)))
        if len(network_intervals) > 1:
            comparable_networks += 1
            overlap = overlap or any(
                max(first[0], second[0]) < min(first[1], second[1])
                for index, first in enumerate(network_intervals)
                for second in network_intervals[index + 1 :]
            )
    if unresolved or overlap:
        decision = "容量区间重叠或尚未闭合，证据不足以区分；不得强行排名。"
    elif comparable_networks:
        decision = "已确认容量区间不重叠；排序仅适用于本次 SLO、链路和服务器环境。"
    else:
        decision = "已报告单台服务器的确认容量区间，不进行跨服务器排名。"
    return {
        "generatedAt": utc_now().isoformat(),
        "capacityUnit": "successful business iterations/second",
        "confidenceLevel": CapacityDraft.model_validate(record.draft_json).slo.confidence_level,
        "networks": networks,
        "decision": decision,
    }


def _advance_capacity_job(study_id: str, settings: Settings) -> None:
    from looper_api.database import SessionLocal

    with SessionLocal() as session:
        record = session.get(CapacityStudyRecord, study_id)
        if record is None or record.status not in {"running", "cancelling", "needs-attention"}:
            return
        if record.status == "cancelling":
            cleanup = _cleanup_all(record, settings)
            execution = dict(record.execution_json)
            execution["cleanup"] = cleanup
            record.execution_json = execution
            cleanup_ok = all(item["status"] == "clean" for item in cleanup)
            record.status = "cancelled" if cleanup_ok else "needs-attention"
            record.completed_at = utc_now()
            _phase(record, "cleanup", "completed" if cleanup_ok else "failed", "取消后的环境清理")
            session.commit()
            return
        runs = list(record.execution_json.get("runs") or [])
        if not runs:
            return
        current = runs[-1]
        experiment = session.get(ExperimentRecord, current.get("experimentId"))
        if experiment is None or ExperimentStatus(experiment.status) not in {
            ExperimentStatus.COMPLETED,
            ExperimentStatus.FAILED,
            ExperimentStatus.CANCELLED,
        }:
            return
        current["status"] = experiment.status
        current["completedAt"] = utc_now().isoformat()
        execution = dict(record.execution_json)
        execution["runs"] = runs
        record.execution_json = execution
        network = str(current["network"])
        if experiment.status != ExperimentStatus.COMPLETED:
            cleanup = _cleanup_all(record, settings)
            execution = dict(record.execution_json)
            execution["cleanup"] = cleanup
            record.execution_json = execution
            cleanup_ok = all(item["status"] == "clean" for item in cleanup)
            record.status = "failed" if cleanup_ok else "needs-attention"
            record.error_code = "capacity_experiment_failed"
            record.error_message = f"{network} capacity experiment ended as {experiment.status}"
            record.completed_at = utc_now()
            session.commit()
            return
        draft = CapacityDraft.model_validate(record.draft_json)
        enabled_networks = list(draft.targets.enabled_networks)
        try:
            network_index = enabled_networks.index(network)
        except ValueError as error:
            raise CapacityError(
                f"completed network {network!r} is absent from the frozen capacity contract"
            ) from error
        _phase(record, network, "completed", f"{network} 容量搜索完成")
        if network_index + 1 < len(enabled_networks):
            next_network = enabled_networks[network_index + 1]
            record.status = "resetting"
            session.commit()
            try:
                reset_capacity_targets(record, settings)
                session.refresh(record)
                if record.status == "cancelling":
                    cleanup = _cleanup_all(record, settings)
                    execution = dict(record.execution_json)
                    execution["cleanup"] = cleanup
                    record.execution_json = execution
                    cleanup_ok = all(item["status"] == "clean" for item in cleanup)
                    record.status = "cancelled" if cleanup_ok else "needs-attention"
                    record.completed_at = utc_now()
                    _phase(
                        record,
                        "cleanup",
                        "completed" if cleanup_ok else "failed",
                        "取消后的环境清理",
                    )
                    session.commit()
                    return
                create_capacity_experiment(session, record, settings, next_network)
                _phase(record, "reset", "completed", "环境已重置")
                _phase(
                    record,
                    next_network,
                    "running",
                    f"正在执行 {next_network} 容量边界搜索",
                )
                session.commit()
            except Exception as error:
                session.rollback()
                record = session.get(CapacityStudyRecord, study_id)
                cleanup = _cleanup_all(record, settings)
                execution = dict(record.execution_json)
                execution["cleanup"] = cleanup
                record.execution_json = execution
                record.status = (
                    "needs-attention"
                    if any(item["status"] != "clean" for item in cleanup)
                    else "failed"
                )
                record.error_code = "capacity_reset_failed"
                record.error_message = str(error)[:16000]
                record.completed_at = utc_now()
                session.commit()
            return
        record.status = "cleaning"
        session.commit()
        cleanup = _cleanup_all(record, settings)
        session.refresh(record)
        cancellation_requested = record.status == "cancelling"
        execution = dict(record.execution_json)
        execution["cleanup"] = cleanup
        record.execution_json = execution
        cleanup_ok = all(item["status"] == "clean" for item in cleanup)
        if cleanup_ok and not cancellation_requested:
            record.report_json = _report_capacity_study(session, record)
            record.status = "completed"
            record.error_code = None
            record.error_message = None
        elif cleanup_ok:
            record.status = "cancelled"
            record.error_code = None
            record.error_message = None
        else:
            record.status = "needs-attention"
            record.error_code = "capacity_cleanup_failed"
            record.error_message = "one or more target environments could not be cleaned"
        record.completed_at = utc_now()
        _phase(record, "cleanup", "completed" if cleanup_ok else "failed", "隔离环境清理验证")
        session.commit()


def _spawn_capacity_job(study_id: str, settings: Settings, action: str) -> None:
    with _capacity_jobs_lock:
        if study_id in _capacity_jobs:
            return
        _capacity_jobs.add(study_id)

    def run() -> None:
        try:
            if action == "prepare":
                _prepare_capacity_job(study_id, settings)
            else:
                _advance_capacity_job(study_id, settings)
        finally:
            with _capacity_jobs_lock:
                _capacity_jobs.discard(study_id)

    threading.Thread(target=run, daemon=True, name=f"capacity-{study_id[-12:]}").start()


def reconcile_capacity_studies(settings: Settings) -> None:
    from looper_api.database import SessionLocal

    with SessionLocal() as session:
        records = list(
            session.scalars(
                select(CapacityStudyRecord).where(
                    CapacityStudyRecord.status.in_(
                        ["queued", "running", "cancelling", "needs-attention"]
                    )
                )
            )
        )
    for record in records:
        if record.status == "queued":
            _spawn_capacity_job(record.id, settings, "prepare")
        elif record.status in {"running", "cancelling"}:
            _spawn_capacity_job(record.id, settings, "advance")


def cancel_capacity_study(session: Session, record: CapacityStudyRecord) -> CapacityStudyRecord:
    from looper_api.scheduler import cancel_experiment

    if record.status not in {"queued", "deploying", "running", "resetting", "cleaning"}:
        raise CapacityError("capacity study is not active", status_code=409)
    for run in record.execution_json.get("runs") or []:
        experiment = session.get(ExperimentRecord, run.get("experimentId"))
        if experiment is not None and experiment.status in {"queued", "running", "paused"}:
            cancel_experiment(session, experiment)
    record.status = "cancelling"
    _phase(record, "cancel", "running", "已请求取消；正在清理隔离环境")
    return record


def retry_capacity_cleanup(session: Session, record: CapacityStudyRecord) -> CapacityStudyRecord:
    if record.status != "needs-attention":
        raise CapacityError("cleanup retry is available only for needs-attention studies")
    record.status = "cancelling"
    record.error_code = None
    record.error_message = None
    _phase(record, "cleanup-retry", "running", "操作员已请求重新清理")
    return record


def recover_interrupted_capacity_studies(session: Session) -> int:
    records = list(
        session.scalars(
            select(CapacityStudyRecord).where(
                CapacityStudyRecord.status.in_(["deploying", "resetting", "cleaning"])
            )
        )
    )
    for record in records:
        record.status = "needs-attention"
        record.error_code = "capacity_control_plane_interrupted"
        record.error_message = (
            "control plane stopped during a remote mutation; verify and retry cleanup"
        )
        _phase(record, "recovery", "failed", "控制平面中断，需要验证并重新清理")
    return len(records)
