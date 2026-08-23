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
                                            "method": "get",
                                            "path": "/health",
                                            "summary": "Health check",
                                            "handlerSymbol": "health",
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
    assert requests[1]["messages"][-1]["role"] == "tool"


@pytest.mark.asyncio
async def test_failed_provider_attempt_is_persisted(db_session, tmp_path: Path) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "do not persist this body"}})

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
    assert record.error_message == "DeepSeek request failed with HTTP 401"
    assert "do not persist" not in record.error_message
