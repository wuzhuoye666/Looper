from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from looper_core.canonical import canonical_digest, canonical_json
from looper_core.contracts import StrictModel
from looper_core.system_opt.config_manifest import (
    ActivationMode,
    ConfigCategory,
    ConfigComponent,
    ConfigItem,
    ConfigManifest,
)
from looper_core.system_opt.domain import ResolvedDomain
from looper_core.system_opt.hypothesis import (
    HYPOTHESIS_SCHEMA,
    HypothesisEvidence,
    HypothesisState,
    OptimizationHypothesis,
    rank_authorized_hypotheses,
)
from looper_core.system_opt.scoring import DiagnosticPriority
from pydantic import Field, ValidationError, model_validator

from looper_api.capacity_evidence import CapacityStudyEvidence
from looper_api.config import Settings
from looper_api.source_discovery import (
    TOOLS,
    SourceDiscoveryError,
    SourceWorkspace,
    source_archive_digest,
)

CODE_HYPOTHESIS_SCHEMA = "looper.code-driven-hypothesis-result/v1alpha1"
CODE_HYPOTHESIS_HARNESS_VERSION = "deepseek-readonly-source-hypothesis/v1alpha1"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_SYSFS_TARGET = re.compile(
    r"^/sys/block/[A-Za-z0-9._:-]+/queue/(?P<control>scheduler|nomerges)$"
)


class HypothesisGenerationIssue(StrictModel):
    stage: Literal["hypothesis-generation"] = "hypothesis-generation"
    code: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(min_length=1, max_length=2000)
    recoverable: bool
    details: dict[str, Any] = Field(default_factory=dict)


class HypothesisGenerationError(RuntimeError):
    def __init__(self, issue: HypothesisGenerationIssue) -> None:
        super().__init__(issue.message)
        self.issue = issue
        self.trace: list[dict[str, Any]] = []


class GeneratedSourceEvidence(StrictModel):
    file: str = Field(min_length=1, max_length=1000)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    claim: str = Field(min_length=1, max_length=2000)
    symbol: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_range(self) -> GeneratedSourceEvidence:
        if self.end_line < self.start_line:
            raise ValueError("end_line cannot precede start_line")
        return self


class GeneratedHypothesis(StrictModel):
    statement: str = Field(min_length=1, max_length=4000)
    candidate_parameters: dict[str, Any] = Field(min_length=1, max_length=1)
    source_evidence: list[GeneratedSourceEvidence] = Field(min_length=1, max_length=1)


class GeneratedHypothesisOutput(StrictModel):
    hypotheses: list[GeneratedHypothesis] = Field(min_length=1, max_length=1)


class CodeDrivenHypothesisResult(StrictModel):
    schema_version: Literal[CODE_HYPOTHESIS_SCHEMA] = CODE_HYPOTHESIS_SCHEMA
    provider: Literal["deepseek"] = "deepseek"
    model: str = Field(min_length=1, max_length=200)
    harness_version: Literal[CODE_HYPOTHESIS_HARNESS_VERSION] = (
        CODE_HYPOTHESIS_HARNESS_VERSION
    )
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runtime_profile_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    configuration_contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    configuration_contract: dict[str, Any]
    hypothesis: OptimizationHypothesis
    trace: list[dict[str, Any]]

    @model_validator(mode="after")
    def validate_configuration_contract(self) -> CodeDrivenHypothesisResult:
        if canonical_digest(self.configuration_contract) != self.configuration_contract_digest:
            raise ValueError("configuration contract digest does not match its payload")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


@dataclass(frozen=True, slots=True)
class AuthorizedHypothesisContext:
    manifest: ConfigManifest
    resolved_domains: dict[str, ResolvedDomain]
    configuration_contract: dict[str, Any]
    configuration_contract_digest: str


def _issue(
    code: str,
    message: str,
    *,
    recoverable: bool = False,
    **details: Any,
) -> HypothesisGenerationError:
    return HypothesisGenerationError(
        HypothesisGenerationIssue(
            code=code,
            message=message,
            recoverable=recoverable,
            details=details,
        )
    )


def _safe_v1_item(item: ConfigItem) -> bool:
    return bool(
        item.category == ConfigCategory.IO
        and item.primary_component == ConfigComponent.STORAGE
        and item.activation == ActivationMode.IMMEDIATE
        and item.searchable
        and item.apply is not None
        and _SAFE_SYSFS_TARGET.fullmatch(item.target)
    )


def authorize_hypothesis_context(
    manifest: ConfigManifest,
    resolved_domains: Mapping[str, ResolvedDomain],
) -> AuthorizedHypothesisContext:
    items = [item for item in manifest.items if _safe_v1_item(item)]
    authorized: dict[str, ResolvedDomain] = {}
    contract_items: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda candidate: candidate.parameter_id):
        domain = resolved_domains.get(item.parameter_id)
        if domain is None:
            continue
        if domain.item_id != item.id or domain.parameter_id != item.parameter_id:
            raise _issue(
                "resolved_domain_identity_mismatch",
                "resolved domain identity does not match the configuration item",
                parameter_id=item.parameter_id,
            )
        authorized[item.parameter_id] = domain
        contract_items.append(
            {
                "parameterId": item.parameter_id,
                "item": item.model_dump(mode="json", exclude_none=False),
                "resolvedDomain": domain.model_dump(mode="json", exclude_none=False),
            }
        )
    if not authorized:
        raise _issue(
            "no_authorized_storage_domain",
            "no verified scheduler or nomerges domain is authorized for this target",
            recoverable=True,
        )
    contract = {
        "schemaVersion": "looper.code-hypothesis-configuration-contract/v1alpha1",
        "manifestDigest": manifest.digest,
        "scope": "single-parameter scheduler-or-nomerges",
        "items": contract_items,
    }
    return AuthorizedHypothesisContext(
        manifest=manifest,
        resolved_domains=authorized,
        configuration_contract=contract,
        configuration_contract_digest=canonical_digest(contract),
    )


def _validate_runtime_profile(
    runtime_profile_digest: str,
    priorities: Sequence[DiagnosticPriority],
) -> None:
    if not _DIGEST.fullmatch(runtime_profile_digest):
        raise _issue(
            "runtime_profile_invalid",
            "runtime profile artifact digest is missing or invalid",
        )
    if not any(priority.component == ConfigComponent.STORAGE.value for priority in priorities):
        raise _issue(
            "runtime_profile_missing",
            "runtime diagnostics do not route a priority to the storage component",
            recoverable=True,
            available_components=sorted({priority.component for priority in priorities}),
        )


def _source_evidence(
    generated: GeneratedSourceEvidence,
    workspace: SourceWorkspace,
    source_digest: str,
) -> HypothesisEvidence:
    content = workspace.files.get(generated.file)
    lines = content.splitlines() if content is not None else []
    if (
        content is None
        or generated.end_line < generated.start_line
        or generated.end_line > len(lines)
    ):
        raise _issue(
            "source_evidence_invalid",
            "generated source citation does not exist in the digest-bound archive",
            file=generated.file,
            start_line=generated.start_line,
            end_line=generated.end_line,
        )
    return HypothesisEvidence(
        kind="source-code",
        digest=source_digest,
        locator=generated.file,
        claim=generated.claim,
        symbol=generated.symbol,
        line_start=generated.start_line,
        line_end=generated.end_line,
    )


def build_authorized_hypothesis(
    generated: GeneratedHypothesis,
    *,
    capacity: CapacityStudyEvidence,
    workspace: SourceWorkspace,
    source_digest: str,
    runtime_profile_digest: str,
    priorities: Sequence[DiagnosticPriority],
    context: AuthorizedHypothesisContext,
) -> OptimizationHypothesis:
    source = _source_evidence(generated.source_evidence[0], workspace, source_digest)
    candidate_digest = canonical_digest(generated.candidate_parameters).split(":", 1)[1][:16]
    hypothesis = OptimizationHypothesis(
        schema_version=HYPOTHESIS_SCHEMA,
        hypothesis_id=f"code-driven.storage.{candidate_digest}",
        statement=generated.statement,
        state=HypothesisState.SUPPORTED_HYPOTHESIS,
        context_digest=capacity.context_digest,
        affected_components=[ConfigComponent.STORAGE.value],
        candidate_parameters=generated.candidate_parameters,
        evidence=[
            HypothesisEvidence(
                kind="runtime-profile",
                digest=runtime_profile_digest,
                locator=f"evidence://runtime-profile/{runtime_profile_digest}",
                claim="Runtime diagnostics route a measured priority to storage.",
            ),
            source,
            HypothesisEvidence(
                kind="configuration-contract",
                digest=context.configuration_contract_digest,
                locator=(
                    "evidence://configuration-contract/"
                    f"{context.configuration_contract_digest}"
                ),
                claim=(
                    "The candidate is inside the target-verified and task-authorized "
                    "scheduler or nomerges domain."
                ),
            ),
        ],
    )
    ranked, rejected = rank_authorized_hypotheses(
        [hypothesis],
        priorities,
        expected_context_digest=capacity.context_digest,
        manifest=context.manifest,
        resolved_domains=context.resolved_domains,
    )
    if not ranked:
        reason = rejected.get(hypothesis.digest, "candidate-rejected")
        code = (
            "runtime_profile_missing"
            if reason == "no-runtime-priority-for-candidate-component"
            else "candidate_not_authorized"
        )
        raise _issue(
            code,
            "generated candidate failed deterministic authorization",
            recoverable=code == "runtime_profile_missing",
            reason=reason,
        )
    return ranked[0]


def _prompt(
    capacity: CapacityStudyEvidence,
    priorities: Sequence[DiagnosticPriority],
    context: AuthorizedHypothesisContext,
) -> list[dict[str, Any]]:
    output_contract = (
        '{"hypotheses":[{"statement":"...","candidate_parameters":'
        '{"system.parameter":value},"source_evidence":[{"file":"...",'
        '"start_line":1,"end_line":1,"claim":"...","symbol":null}]}]}'
    )
    return [
        {
            "role": "system",
            "content": (
                "You form one falsifiable performance hypothesis from source evidence. "
                "Use only the supplied read-only source tools. Do not infer a bottleneck "
                "from source alone: the supplied runtime priorities are the measured "
                "routing evidence. Choose exactly one parameter and one value from the "
                "authorized configuration contract, cite exactly one precise source line "
                f"range, and return only this JSON shape: {output_contract}"
            ),
        },
        {
            "role": "user",
            "content": canonical_json(
                {
                    "task": "Propose one source-grounded storage candidate for a capacity test.",
                    "capacityContextDigest": capacity.context_digest,
                    "runtimePriorities": [
                        priority.model_dump(mode="json") for priority in priorities
                    ],
                    "authorizedConfiguration": context.configuration_contract,
                    "constraints": {
                        "hypotheses": 1,
                        "parametersPerHypothesis": 1,
                        "sourceCitationsPerHypothesis": 1,
                    },
                }
            ),
        },
    ]


def _parse_output(content: str) -> GeneratedHypothesisOutput:
    normalized = content.strip()
    if normalized.startswith("```"):
        first_newline = normalized.find("\n")
        if first_newline != -1 and normalized.endswith("```"):
            normalized = normalized[first_newline + 1 : -3].strip()
    first_brace = normalized.find("{")
    last_brace = normalized.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        normalized = normalized[first_brace : last_brace + 1]
    return GeneratedHypothesisOutput.model_validate(json.loads(normalized))


async def generate_code_driven_hypothesis(
    *,
    capacity: CapacityStudyEvidence,
    source_archive: bytes,
    runtime_profile_digest: str,
    priorities: Sequence[DiagnosticPriority],
    manifest: ConfigManifest,
    resolved_domains: Mapping[str, ResolvedDomain],
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> CodeDrivenHypothesisResult:
    _validate_runtime_profile(runtime_profile_digest, priorities)
    actual_source_digest = source_archive_digest(source_archive)
    if actual_source_digest != capacity.identity["source_digest"]:
        raise _issue(
            "source_digest_mismatch",
            "source archive does not match the baseline capacity evidence",
            expected=capacity.identity["source_digest"],
            actual=actual_source_digest,
        )
    context = authorize_hypothesis_context(manifest, resolved_domains)
    try:
        workspace = SourceWorkspace.from_zip(source_archive, settings)
    except SourceDiscoveryError as error:
        raise _issue(
            "source_archive_invalid",
            "digest-bound source archive cannot be inspected safely",
            source_error_code=error.code,
        ) from error
    if not settings.deepseek_api_key.strip():
        raise _issue(
            "deepseek_not_configured",
            "DeepSeek is not configured for code-driven hypothesis generation",
            recoverable=True,
        )

    messages = _prompt(capacity, priorities, context)
    trace: list[dict[str, Any]] = []
    repairs = 0
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))
    try:
        for round_number in range(1, settings.source_discovery_max_tool_rounds + 1):
            response = await http.post(
                f"{str(settings.deepseek_base_url).rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
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
                raise _issue(
                    "deepseek_request_failed",
                    "DeepSeek hypothesis request failed",
                    recoverable=True,
                    status_code=response.status_code,
                )
            try:
                choice = response.json()["choices"][0]
                message = choice["message"]
            except (ValueError, KeyError, IndexError, TypeError) as error:
                raise _issue(
                    "deepseek_invalid_response",
                    "DeepSeek returned an invalid response envelope",
                    recoverable=True,
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
                        if not isinstance(arguments, dict):
                            raise ValueError
                        result, metadata = workspace.tool(name, arguments)
                    except (
                        KeyError,
                        ValueError,
                        TypeError,
                        RuntimeError,
                        SourceDiscoveryError,
                    ) as error:
                        name = call.get("function", {}).get("name", "unknown")
                        result = {"error": str(error)}
                        metadata = {"error": "invalid_tool_call"}
                    trace.append(
                        {
                            "round": round_number,
                            "tool": name,
                            "arguments": arguments,
                            "result": metadata,
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", "missing"),
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                continue
            if choice.get("finish_reason") == "length":
                raise _issue(
                    "deepseek_output_truncated",
                    "DeepSeek hypothesis output was truncated",
                    recoverable=True,
                )
            content = message.get("content")
            if not isinstance(content, str):
                raise _issue(
                    "deepseek_missing_output",
                    "DeepSeek did not return a final JSON object",
                    recoverable=True,
                )
            if not trace:
                raise _issue(
                    "deepseek_skipped_source_tools",
                    "DeepSeek returned a candidate without inspecting source files",
                )
            try:
                output = _parse_output(content)
                hypothesis = build_authorized_hypothesis(
                    output.hypotheses[0],
                    capacity=capacity,
                    workspace=workspace,
                    source_digest=actual_source_digest,
                    runtime_profile_digest=runtime_profile_digest,
                    priorities=priorities,
                    context=context,
                )
            except (ValueError, ValidationError, HypothesisGenerationError) as error:
                if isinstance(error, HypothesisGenerationError) and error.issue.code not in {
                    "source_evidence_invalid",
                    "candidate_not_authorized",
                }:
                    raise
                if repairs >= 2:
                    raise _issue(
                        "deepseek_contract_invalid",
                        (
                            "DeepSeek output did not pass deterministic evidence and "
                            "authorization checks"
                        ),
                    ) from error
                repairs += 1
                detail = (
                    error.issue.code
                    if isinstance(error, HypothesisGenerationError)
                    else type(error).__name__
                )
                messages.extend(
                    [
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": (
                                "The JSON failed deterministic validation "
                                f"({detail}). Re-read the exact source range and return one "
                                "candidate inside the supplied authorized domain."
                            ),
                        },
                    ]
                )
                continue
            return CodeDrivenHypothesisResult(
                model=settings.deepseek_model,
                source_digest=actual_source_digest,
                runtime_profile_digest=runtime_profile_digest,
                configuration_contract_digest=context.configuration_contract_digest,
                configuration_contract=context.configuration_contract,
                hypothesis=hypothesis,
                trace=trace,
            )
        raise _issue(
            "deepseek_round_limit",
            "DeepSeek exceeded the configured hypothesis tool round limit",
            recoverable=True,
        )
    except HypothesisGenerationError as error:
        error.trace = trace
        raise
    except httpx.HTTPError as error:
        wrapped = _issue(
            "deepseek_unreachable",
            "DeepSeek could not be reached for hypothesis generation",
            recoverable=True,
        )
        wrapped.trace = trace
        raise wrapped from error
    finally:
        if owns_client:
            await http.aclose()


__all__ = [
    "CODE_HYPOTHESIS_HARNESS_VERSION",
    "CODE_HYPOTHESIS_SCHEMA",
    "AuthorizedHypothesisContext",
    "CodeDrivenHypothesisResult",
    "GeneratedHypothesis",
    "GeneratedHypothesisOutput",
    "GeneratedSourceEvidence",
    "HypothesisGenerationError",
    "HypothesisGenerationIssue",
    "authorize_hypothesis_context",
    "build_authorized_hypothesis",
    "generate_code_driven_hypothesis",
]
