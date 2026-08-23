from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

import httpx
from looper_core.canonical import canonical_digest, new_id, utc_now
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from looper_api.config import Settings
from looper_api.models import SourceDiscoveryRecord

CONTRACT_VERSION = "looper.dev/interface-contract/v1"
HARNESS_VERSION = "deepseek-readonly-tools/v1"
_SKIPPED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "__pycache__",
    ".next",
}
_SECRET_NAMES = {".env", "id_rsa", "id_ed25519", "credentials", "credentials.json", "secrets.json"}
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}


class SourceDiscoveryError(Exception):
    def __init__(
        self, message: str, *, status_code: int = 422, code: str = "source_discovery_error"
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class EvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str
    startLine: int = Field(ge=1)
    endLine: int = Field(ge=1)


class ParameterInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str
    location: Literal["path", "query", "header", "cookie"] = Field(alias="in")
    required: bool = False
    contract_schema: dict[str, Any] = Field(default_factory=dict, alias="schema")


class RequestBodyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    required: bool = False
    contentTypes: list[str] = Field(default_factory=list)
    contract_schema: dict[str, Any] = Field(default_factory=dict, alias="schema")


class ResponseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    statusCode: str
    contentTypes: list[str] = Field(default_factory=list)
    contract_schema: dict[str, Any] = Field(default_factory=dict, alias="schema")


class InterfaceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["http"] = "http"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    path: str
    summary: str = ""
    handlerSymbol: str | None = None
    parameters: list[ParameterInput] = Field(default_factory=list)
    requestBody: RequestBodyInput | None = None
    responses: list[ResponseInput] = Field(default_factory=list)
    authentication: list[str] = Field(default_factory=list)
    sideEffect: Literal["none", "read", "write", "delete", "unknown"] = "unknown"
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceInput] = Field(min_length=1)
    unresolved: list[str] = Field(default_factory=list)

    @field_validator("path")
    @classmethod
    def validate_http_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("HTTP interface paths must start with /")
        return value


class AgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interfaces: list[InterfaceInput]
    unresolved: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class SourceWorkspace:
    files: dict[str, str]
    manifest: list[dict[str, Any]]
    excluded: list[dict[str, str]]

    @classmethod
    def from_zip(cls, archive: bytes, settings: Settings) -> SourceWorkspace:
        if len(archive) > settings.source_discovery_max_archive_bytes:
            raise SourceDiscoveryError(
                "ZIP exceeds configured archive size limit",
                status_code=413,
                code="archive_too_large",
            )
        if not archive.startswith(b"PK"):
            raise SourceDiscoveryError(
                "only ZIP source archives are accepted", code="invalid_archive_type"
            )
        files: dict[str, str] = {}
        manifest: list[dict[str, Any]] = []
        excluded: list[dict[str, str]] = []
        expanded = 0
        try:
            source = zipfile.ZipFile(io.BytesIO(archive))
        except zipfile.BadZipFile as error:
            raise SourceDiscoveryError("archive is not a valid ZIP", code="invalid_zip") from error
        with source:
            entries = source.infolist()
            if len(entries) > settings.source_discovery_max_files:
                raise SourceDiscoveryError(
                    "ZIP contains too many entries", status_code=413, code="too_many_files"
                )
            for info in entries:
                raw_path = info.filename.replace("\\", "/")
                path = PurePosixPath(raw_path)
                if info.is_dir():
                    continue
                if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", raw_path):
                    raise SourceDiscoveryError(
                        "ZIP contains an unsafe path", code="unsafe_archive_path"
                    )
                normalized = path.as_posix().lstrip("./")
                if not normalized or normalized in files:
                    raise SourceDiscoveryError(
                        "ZIP contains duplicate or empty paths", code="duplicate_archive_path"
                    )
                mode = info.external_attr >> 16
                if mode & 0o170000 == 0o120000:
                    raise SourceDiscoveryError(
                        "ZIP symbolic links are not accepted", code="archive_symlink"
                    )
                if info.flag_bits & 0x1:
                    raise SourceDiscoveryError(
                        "encrypted ZIP entries are not accepted", code="encrypted_archive"
                    )
                expanded += info.file_size
                if expanded > settings.source_discovery_max_expanded_bytes:
                    raise SourceDiscoveryError(
                        "ZIP expanded size exceeds configured limit",
                        status_code=413,
                        code="expanded_archive_too_large",
                    )
                lower_parts = [part.casefold() for part in path.parts]
                lower_name = path.name.casefold()
                reason = None
                if any(part in _SKIPPED_DIRS for part in lower_parts[:-1]):
                    reason = "generated_or_dependency_directory"
                elif (
                    lower_name in _SECRET_NAMES
                    or lower_name.startswith(".env.")
                    or path.suffix.casefold() in _SECRET_SUFFIXES
                ):
                    reason = "sensitive_filename"
                data = source.read(info)
                if reason is None and (b"\x00" in data[:8192] or len(data) > 1024 * 1024):
                    reason = "binary_or_oversized_file"
                if reason:
                    excluded.append({"path": normalized, "reason": reason})
                    continue
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    excluded.append({"path": normalized, "reason": "non_utf8_file"})
                    continue
                files[normalized] = text
                manifest.append(
                    {
                        "path": normalized,
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
        if not files:
            raise SourceDiscoveryError(
                "ZIP has no readable source files after safety filtering", code="no_readable_source"
            )
        return cls(files=files, manifest=manifest, excluded=excluded)

    def tool(self, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if name == "list_files":
            pattern = str(arguments.get("pattern", "*"))[:200]
            limit = min(max(int(arguments.get("limit", 200)), 1), 500)
            paths = [p for p in sorted(self.files) if fnmatch.fnmatch(p, pattern)][:limit]
            return {"files": paths, "truncated": len(paths) == limit}, {"count": len(paths)}
        if name == "search_code":
            query = str(arguments.get("query", ""))[:200]
            if not query:
                raise SourceDiscoveryError(
                    "search query cannot be empty", code="invalid_tool_arguments"
                )
            limit = min(max(int(arguments.get("limit", 50)), 1), 100)
            matches: list[dict[str, Any]] = []
            needle = query.casefold()
            for path, content in sorted(self.files.items()):
                for number, line in enumerate(content.splitlines(), 1):
                    if needle in line.casefold():
                        matches.append({"file": path, "line": number, "text": line[:400]})
                        if len(matches) >= limit:
                            return {"matches": matches, "truncated": True}, {"count": len(matches)}
            return {"matches": matches, "truncated": False}, {"count": len(matches)}
        if name == "read_file":
            path = str(arguments.get("path", ""))
            if path not in self.files:
                raise SourceDiscoveryError(
                    "requested source file does not exist", code="invalid_tool_arguments"
                )
            lines = self.files[path].splitlines()
            start = min(max(int(arguments.get("startLine", 1)), 1), max(len(lines), 1))
            end = min(
                max(int(arguments.get("endLine", start + 199)), start), min(len(lines), start + 399)
            )
            selected = [
                {"line": number, "text": lines[number - 1][:1000]}
                for number in range(start, end + 1)
            ]
            return {"file": path, "startLine": start, "endLine": end, "lines": selected}, {
                "file": path,
                "startLine": start,
                "endLine": end,
            }
        raise SourceDiscoveryError("unknown harness tool", code="unknown_tool")


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List repository files by glob without executing code.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}, "limit": {"type": "integer"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Literal case-insensitive search across source lines.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a bounded line range from one source file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "startLine": {"type": "integer"},
                    "endLine": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
]


async def run_deepseek_harness(
    workspace: SourceWorkspace, settings: Settings, client: httpx.AsyncClient | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not settings.deepseek_api_key.strip():
        raise SourceDiscoveryError(
            "DeepSeek is not configured; set LOOPER_DEEPSEEK_API_KEY",
            status_code=503,
            code="deepseek_not_configured",
        )
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are a source interface discovery agent. Never claim an interface "
                "without file and line evidence. Use only the supplied read-only tools. "
                "Do not ask to execute code or access networks. Your final answer must be "
                "one JSON object matching: {interfaces:[{protocol,method,path,summary,"
                "handlerSymbol,parameters:[{name,in,required,schema}],requestBody:"
                "{required,contentTypes,schema}|null,responses:[{statusCode,contentTypes,"
                "schema}],authentication,sideEffect,confidence,evidence:[{file,startLine,"
                "endLine}],unresolved:[]}],unresolved:[]}. HTTP methods must be uppercase."
            ),
        },
        {
            "role": "user",
            "content": (
                "Discover externally callable HTTP interfaces in this source archive. "
                "Inspect framework registration, routers, handlers, auth, and side effects. "
                "Return JSON only after gathering evidence. If none exist, return an empty "
                "interfaces array."
            ),
        },
    ]
    trace: list[dict[str, Any]] = []
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
                    "tool_choice": "auto",
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                },
            )
            if response.status_code >= 400:
                raise SourceDiscoveryError(
                    f"DeepSeek request failed with HTTP {response.status_code}",
                    status_code=502,
                    code="deepseek_request_failed",
                )
            try:
                body = response.json()
                message = body["choices"][0]["message"]
            except (ValueError, KeyError, IndexError, TypeError) as error:
                raise SourceDiscoveryError(
                    "DeepSeek returned an invalid response envelope",
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
                        if not isinstance(arguments, dict):
                            raise ValueError
                        result, metadata = workspace.tool(name, arguments)
                    except (KeyError, ValueError, TypeError, SourceDiscoveryError) as error:
                        result = {"error": str(error)}
                        metadata = {"error": getattr(error, "code", "invalid_tool_call")}
                        name = call.get("function", {}).get("name", "unknown")
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
            content = message.get("content")
            if not isinstance(content, str):
                raise SourceDiscoveryError(
                    "DeepSeek did not return a final JSON object",
                    status_code=502,
                    code="deepseek_missing_output",
                )
            try:
                output = AgentOutput.model_validate(json.loads(content))
            except (ValueError, ValidationError) as error:
                raise SourceDiscoveryError(
                    "DeepSeek output does not match the interface contract",
                    status_code=502,
                    code="deepseek_contract_invalid",
                ) from error
            return build_contract(output, workspace, settings), trace
        raise SourceDiscoveryError(
            "DeepSeek exceeded the configured tool round limit",
            status_code=502,
            code="deepseek_round_limit",
        )
    except httpx.HTTPError as error:
        raise SourceDiscoveryError(
            "DeepSeek could not be reached", status_code=502, code="deepseek_unreachable"
        ) from error
    finally:
        if owns_client:
            await http.aclose()


def build_contract(
    output: AgentOutput, workspace: SourceWorkspace, settings: Settings
) -> dict[str, Any]:
    interfaces: list[dict[str, Any]] = []
    for item in output.interfaces:
        evidence = []
        for citation in item.evidence:
            content = workspace.files.get(citation.file)
            lines = content.splitlines() if content is not None else []
            if (
                content is None
                or citation.endLine < citation.startLine
                or citation.endLine > len(lines)
            ):
                raise SourceDiscoveryError(
                    "DeepSeek cited source evidence that does not exist",
                    status_code=502,
                    code="deepseek_evidence_invalid",
                )
            excerpt = "\n".join(lines[citation.startLine - 1 : citation.endLine])
            evidence.append(
                {**citation.model_dump(), "excerptDigest": canonical_digest({"text": excerpt})}
            )
        identity = canonical_digest(
            {
                "protocol": item.protocol,
                "method": item.method,
                "path": item.path,
                "evidence": evidence,
            }
        ).removeprefix("sha256:")[:16]
        interfaces.append(
            {
                "id": f"interface-{identity}",
                "protocol": item.protocol,
                "method": item.method.upper(),
                "path": item.path,
                "summary": item.summary,
                "handler": {"symbol": item.handlerSymbol},
                "parameters": [
                    parameter.model_dump(by_alias=True) for parameter in item.parameters
                ],
                "requestBody": item.requestBody.model_dump(by_alias=True)
                if item.requestBody
                else None,
                "responses": [response.model_dump(by_alias=True) for response in item.responses],
                "authentication": item.authentication,
                "sideEffect": item.sideEffect,
                "confidence": item.confidence,
                "evidence": evidence,
                "unresolved": item.unresolved,
            }
        )
    return {
        "apiVersion": CONTRACT_VERSION,
        "kind": "InterfaceContract",
        "metadata": {
            "provider": "deepseek",
            "model": settings.deepseek_model,
            "harnessVersion": HARNESS_VERSION,
        },
        "spec": {"interfaces": interfaces, "unresolved": output.unresolved},
    }


def discovery_view(record: SourceDiscoveryRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "archiveName": record.archive_name,
        "sourceDigest": record.source_digest,
        "status": record.status,
        "provider": record.provider,
        "model": record.model,
        "fileManifest": record.file_manifest_json,
        "excludedFiles": record.excluded_files_json,
        "contract": record.contract_json,
        "trace": record.trace_json,
        "error": {"code": record.error_code, "message": record.error_message}
        if record.error_code
        else None,
        "createdAt": record.created_at.isoformat(),
        "completedAt": record.completed_at.isoformat() if record.completed_at else None,
    }


async def create_discovery(
    session: Session,
    archive_name: str,
    archive: bytes,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> SourceDiscoveryRecord:
    workspace = SourceWorkspace.from_zip(archive, settings)
    record = SourceDiscoveryRecord(
        id=new_id("discovery"),
        archive_name=archive_name[:255],
        source_digest=canonical_digest({"archiveSha256": hashlib.sha256(archive).hexdigest()}),
        status="running",
        provider="deepseek",
        model=settings.deepseek_model,
        file_manifest_json=workspace.manifest,
        excluded_files_json=workspace.excluded,
        contract_json=None,
        trace_json=[],
        error_code=None,
        error_message=None,
        created_at=utc_now(),
        completed_at=None,
    )
    session.add(record)
    session.commit()
    try:
        contract, trace = await run_deepseek_harness(workspace, settings, client)
        contract["metadata"]["sourceDigest"] = record.source_digest
        record.contract_json = contract
        record.trace_json = trace
        record.status = "completed"
    except SourceDiscoveryError as error:
        record.status = "failed"
        record.error_code = error.code
        record.error_message = str(error)
        record.completed_at = utc_now()
        session.commit()
        raise
    record.completed_at = utc_now()
    session.commit()
    return record


def list_discoveries(session: Session, limit: int = 30) -> list[SourceDiscoveryRecord]:
    return list(
        session.scalars(
            select(SourceDiscoveryRecord)
            .order_by(SourceDiscoveryRecord.created_at.desc())
            .limit(limit)
        )
    )
