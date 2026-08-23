from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from looper_api.config import Settings
from looper_api.models import SourceDiscoveryRecord
from looper_api.source_discovery import (
    CONTRACT_VERSION,
    SourceDiscoveryError,
    SourceWorkspace,
    create_discovery,
    recover_interrupted_discoveries,
    run_deepseek_harness,
)


def archive(entries: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as source:
        for path, content in entries.items():
            source.writestr(path, content)
    return output.getvalue()


def settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        data_dir=tmp_path,
        deepseek_api_key="test-key",
        deepseek_model="deepseek-test",
        **overrides,
    )


def test_zip_workspace_filters_sensitive_and_generated_files(tmp_path: Path) -> None:
    workspace = SourceWorkspace.from_zip(
        archive(
            {
                "app.py": "from fastapi import FastAPI\napp = FastAPI()\n",
                ".env.production": "TOKEN=secret\n",
                "node_modules/package/index.js": "generated\n",
            }
        ),
        settings(tmp_path),
    )
    assert list(workspace.files) == ["app.py"]
    assert {item["reason"] for item in workspace.excluded} == {
        "sensitive_filename",
        "generated_or_dependency_directory",
    }


def test_zip_workspace_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(SourceDiscoveryError, match="unsafe path") as raised:
        SourceWorkspace.from_zip(archive({"../outside.py": "bad"}), settings(tmp_path))
    assert raised.value.code == "unsafe_archive_path"


def test_parameter_locations_are_normalized() -> None:
    from looper_api.source_discovery import InterfaceInput, ParameterInput, ResponseInput

    path_parameter = ParameterInput.model_validate({"name": "id", "in": "Path Parameter"})
    request_body = ParameterInput.model_validate({"name": "body", "in": "requestBody"})
    unresolved = ParameterInput.model_validate({"name": "value", "in": "unknown"})
    assert path_parameter.location == "path"
    assert request_body.location == "body"
    assert unresolved.location == "unknown"
    response = ResponseInput.model_validate(
        {"statusCode": 200, "contentTypes": "application/json", "schema": "User response"}
    )
    assert response.contentTypes == ["application/json"]
    assert response.contract_schema == {"description": "User response"}
    interface = InterfaceInput.model_validate(
        {
            "method": "GET",
            "path": "/users",
            "authentication": "Bearer token",
            "confidence": "high",
            "evidence": [{"file": "app.py", "startLine": 1, "endLine": 1}],
        }
    )
    assert interface.authentication == ["Bearer token"]
    assert interface.confidence == 0.85


@pytest.mark.asyncio
async def test_harness_executes_only_read_tools_and_validates_evidence(tmp_path: Path) -> None:
    workspace = SourceWorkspace.from_zip(
        archive(
            {
                "app.py": (
                    "from fastapi import FastAPI\n"
                    "app = FastAPI()\n"
                    "@app.get('/health')\n"
                    "def health():\n"
                    "    return {'ok': True}\n"
                )
            }
        ),
        settings(tmp_path),
    )
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": json.dumps(
                                                {"path": "app.py", "startLine": 1, "endLine": 5}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "interfaces": [
                                        {
                                            "protocol": "http",
                                            "method": "GET",
                                            "path": "/health",
                                            "summary": "Health check",
                                            "handlerSymbol": "health",
                                            "parameters": [],
                                            "requestBody": None,
                                            "responses": [
                                                {
                                                    "statusCode": "200",
                                                    "contentTypes": ["application/json"],
                                                    "schema": {"type": "object"},
                                                }
                                            ],
                                            "authentication": [],
                                            "sideEffect": "none",
                                            "confidence": 0.99,
                                            "evidence": [
                                                {"file": "app.py", "startLine": 3, "endLine": 5}
                                            ],
                                            "unresolved": [],
                                        }
                                    ],
                                    "unresolved": [],
                                }
                            ),
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        contract, trace = await run_deepseek_harness(workspace, settings(tmp_path), client)

    assert contract["apiVersion"] == CONTRACT_VERSION
    assert contract["spec"]["interfaces"][0]["path"] == "/health"
    assert contract["spec"]["interfaces"][0]["responses"][0]["statusCode"] == "200"
    assert contract["spec"]["interfaces"][0]["id"].startswith("interface-")
    assert contract["spec"]["interfaces"][0]["evidence"][0]["excerptDigest"].startswith("sha256:")
    assert trace == [
        {
            "round": 1,
            "tool": "read_file",
            "arguments": {"path": "app.py", "startLine": 1, "endLine": 5},
            "result": {"file": "app.py", "startLine": 1, "endLine": 5},
        }
    ]
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[0]["max_tokens"] == 16384
    assert requests[0]["tool_choice"] == "required"
    assert requests[1]["tool_choice"] == "auto"
    assert requests[1]["messages"][-1]["role"] == "tool"


@pytest.mark.asyncio
async def test_failed_provider_attempt_is_persisted(db_session, tmp_path: Path) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "code": "invalid_request_error",
                    "message": "Invalid parameter for test-key",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceDiscoveryError) as raised:
            await create_discovery(
                db_session,
                "service.zip",
                archive({"app.py": "print('source is never executed')\n"}),
                settings(tmp_path),
                client,
            )
    assert raised.value.code == "deepseek_request_failed"
    record = db_session.query(SourceDiscoveryRecord).one()
    assert record.status == "failed"
    assert record.error_message == (
        "DeepSeek request failed with HTTP 401 "
        "(invalid_request_error: Invalid parameter for [REDACTED])"
    )
    assert "test-key" not in record.error_message


@pytest.mark.asyncio
async def test_fenced_json_is_accepted(db_session, tmp_path: Path) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-list",
                                        "type": "function",
                                        "function": {
                                            "name": "list_files",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "Analysis complete.\n```json\n"
                                '{"interfaces": [], "unresolved": []}\n```\nDone.'
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        record = await create_discovery(
            db_session,
            "fenced.zip",
            archive({"app.py": "print('not executed')\n"}),
            settings(tmp_path),
            client,
        )
    assert record.status == "completed"
    assert record.trace_json[0]["tool"] == "list_files"


@pytest.mark.asyncio
async def test_tool_trace_is_persisted_when_final_output_fails(db_session, tmp_path: Path) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        message: dict[str, object]
        if calls == 1:
            message = {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-search",
                        "type": "function",
                        "function": {
                            "name": "search_code",
                            "arguments": '{"query":"route"}',
                        },
                    }
                ],
            }
        else:
            message = {"content": "not json"}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceDiscoveryError):
            await create_discovery(
                db_session,
                "invalid-final.zip",
                archive({"app.py": "route = '/health'\n"}),
                settings(tmp_path),
                client,
            )
    record = (
        db_session.query(SourceDiscoveryRecord).filter_by(archive_name="invalid-final.zip").one()
    )
    assert record.status == "failed"
    assert record.trace_json[0]["tool"] == "search_code"


@pytest.mark.asyncio
async def test_invalid_final_json_gets_bounded_self_repair(tmp_path: Path) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            message: dict[str, object] = {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-list",
                        "type": "function",
                        "function": {"name": "list_files", "arguments": "{}"},
                    }
                ],
            }
        elif calls == 2:
            message = {"content": "I found no HTTP routes."}
        else:
            payload = json.loads(request.content)
            assert "not a valid JSON object" in payload["messages"][-1]["content"]
            message = {"content": '{"interfaces": [], "unresolved": []}'}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    workspace = SourceWorkspace.from_zip(
        archive({"README.md": "No server here.\n"}), settings(tmp_path)
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        contract, trace = await run_deepseek_harness(workspace, settings(tmp_path), client)
    assert calls == 3
    assert contract["spec"]["interfaces"] == []
    assert trace[0]["tool"] == "list_files"


def test_running_discoveries_are_failed_during_startup_recovery(db_session, tmp_path: Path) -> None:
    workspace = SourceWorkspace.from_zip(
        archive({"app.py": "print('not executed')\n"}), settings(tmp_path)
    )
    from looper_core.canonical import canonical_digest, new_id, utc_now

    record = SourceDiscoveryRecord(
        id=new_id("discovery"),
        archive_name="interrupted.zip",
        source_digest=canonical_digest({"test": "interrupted"}),
        status="running",
        provider="deepseek",
        model="deepseek-test",
        file_manifest_json=workspace.manifest,
        excluded_files_json=[],
        contract_json=None,
        trace_json=[],
        error_code=None,
        error_message=None,
        created_at=utc_now(),
        completed_at=None,
    )
    db_session.add(record)
    db_session.commit()
    assert recover_interrupted_discoveries(db_session) == 1
    db_session.commit()
    assert record.status == "failed"
    assert record.error_code == "source_discovery_interrupted"
