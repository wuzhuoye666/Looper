from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import httpx
import pytest
from looper_api.capacity_evidence import (
    CAPACITY_EVIDENCE_SCHEMA,
    CapacityStudyEvidence,
    ResolvedCapacityFrontier,
)
from looper_api.code_hypothesis import (
    GeneratedHypothesis,
    GeneratedSourceEvidence,
    HypothesisGenerationError,
    authorize_hypothesis_context,
    build_authorized_hypothesis,
    generate_code_driven_hypothesis,
)
from looper_api.config import Settings
from looper_api.source_discovery import SourceWorkspace, source_archive_digest
from looper_core.canonical import canonical_digest
from looper_core.system_opt.demo import build_demo_manifest, resolve_demo_domains
from looper_core.system_opt.hypothesis import HypothesisState, hypothesis_context_digest
from looper_core.system_opt.scoring import DiagnosticPriority


def _digest(seed: str) -> str:
    return canonical_digest({"seed": seed})


def _archive() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "src/storage.py",
            "def submit(request):\n"
            "    # requests are committed synchronously\n"
            "    return device.write(request)\n",
        )
    return output.getvalue()


def _capacity(source_digest: str) -> CapacityStudyEvidence:
    identity = {
        "source_digest": source_digest,
        "workload_digest": _digest("workload"),
        "slo_digest": _digest("slo"),
        "environment_digest": _digest("environment"),
        "network": "internal",
        "target_id": "target-a",
        "capacity_unit": "successful business iterations/second",
        "confidence_level": "0.95",
        "measurement_contract_digest": _digest("measurement"),
    }
    return CapacityStudyEvidence(
        schema_version=CAPACITY_EVIDENCE_SCHEMA,
        study_id="study-a",
        experiment_id="experiment-a",
        target_id="target-a",
        network="internal",
        workload_id="business-iteration",
        metric_id="committed_tps",
        report_digest=_digest("report"),
        study_contract_digest=_digest("study"),
        experiment_contract_digest=_digest("experiment"),
        benchmark_manifest_digest=_digest("benchmark"),
        frontier=ResolvedCapacityFrontier(
            status="resolved", confirmed_pass=90, confirmed_fail=100
        ),
        control_frontiers={},
        active_target_ids=["target-a"],
        identity=identity,
        context_digest=hypothesis_context_digest(identity),
    )


def _priority(component: str = "storage") -> DiagnosticPriority:
    return DiagnosticPriority(
        metric_id=f"{component}.pressure",
        component=component,
        pressure=0.9,
        adverse_change=0.5,
        persistence=0.8,
        confidence=0.95,
        pareto_rank=1,
    )


def _generated(value: str = "none", *, line: int = 2) -> GeneratedHypothesis:
    return GeneratedHypothesis(
        statement="Reducing block scheduler work may move the measured capacity frontier.",
        candidate_parameters={"system.storage-scheduler": value},
        source_evidence=[
            GeneratedSourceEvidence(
                file="src/storage.py",
                start_line=line,
                end_line=line,
                claim="The capacity workload commits storage requests synchronously.",
                symbol="submit",
            )
        ],
    )


def _settings(**updates: Any) -> Settings:
    values: dict[str, Any] = {
        "deepseek_api_key": "test-key",
        "source_discovery_max_tool_rounds": 4,
        "data_dir": ".test-code-hypothesis",
    }
    values.update(updates)
    return Settings(_env_file=None, **values)


def test_v1_authorization_exposes_only_dynamic_scheduler_or_nomerges_domains() -> None:
    manifest = build_demo_manifest()
    domains = resolve_demo_domains(manifest)

    context = authorize_hypothesis_context(manifest, domains)

    assert set(context.resolved_domains) == {"system.storage-scheduler"}
    assert [item["parameterId"] for item in context.configuration_contract["items"]] == [
        "system.storage-scheduler"
    ]
    assert context.configuration_contract["scope"] == "single-parameter scheduler-or-nomerges"


def test_authorized_builder_binds_fixed_state_digests_and_exact_source_lines() -> None:
    archive = _archive()
    capacity = _capacity(source_archive_digest(archive))
    manifest = build_demo_manifest()
    context = authorize_hypothesis_context(manifest, resolve_demo_domains(manifest))
    workspace = SourceWorkspace.from_zip(archive, _settings())

    hypothesis = build_authorized_hypothesis(
        _generated(),
        capacity=capacity,
        workspace=workspace,
        source_digest=source_archive_digest(archive),
        runtime_profile_digest=_digest("runtime"),
        priorities=[_priority()],
        context=context,
    )

    assert hypothesis.state == HypothesisState.SUPPORTED_HYPOTHESIS
    assert hypothesis.affected_components == ["storage"]
    assert hypothesis.context_digest == capacity.context_digest
    assert {item.kind for item in hypothesis.evidence} == {
        "runtime-profile",
        "source-code",
        "configuration-contract",
    }
    source = next(item for item in hypothesis.evidence if item.kind == "source-code")
    assert source.digest == source_archive_digest(archive)
    assert (source.locator, source.line_start, source.line_end) == ("src/storage.py", 2, 2)


def test_builder_rejects_value_outside_target_authorized_domain() -> None:
    archive = _archive()
    capacity = _capacity(source_archive_digest(archive))
    manifest = build_demo_manifest()
    context = authorize_hypothesis_context(manifest, resolve_demo_domains(manifest))

    with pytest.raises(HypothesisGenerationError) as raised:
        build_authorized_hypothesis(
            _generated("kyber"),
            capacity=capacity,
            workspace=SourceWorkspace.from_zip(archive, _settings()),
            source_digest=source_archive_digest(archive),
            runtime_profile_digest=_digest("runtime"),
            priorities=[_priority()],
            context=context,
        )

    assert raised.value.issue.code == "candidate_not_authorized"
    assert raised.value.issue.details["reason"].startswith("value-outside-resolved-domain")


@pytest.mark.asyncio
async def test_generation_stops_before_provider_when_runtime_profile_is_not_routed() -> None:
    archive = _archive()
    manifest = build_demo_manifest()

    with pytest.raises(HypothesisGenerationError) as raised:
        await generate_code_driven_hypothesis(
            capacity=_capacity(source_archive_digest(archive)),
            source_archive=archive,
            runtime_profile_digest=_digest("runtime"),
            priorities=[_priority("cpu")],
            manifest=manifest,
            resolved_domains=resolve_demo_domains(manifest),
            settings=_settings(),
        )

    assert raised.value.issue.code == "runtime_profile_missing"
    assert raised.value.trace == []


@pytest.mark.asyncio
async def test_generation_stops_on_missing_or_changed_source_archive() -> None:
    manifest = build_demo_manifest()
    archive = _archive()
    with pytest.raises(HypothesisGenerationError) as changed:
        await generate_code_driven_hypothesis(
            capacity=_capacity(_digest("different-source")),
            source_archive=archive,
            runtime_profile_digest=_digest("runtime"),
            priorities=[_priority()],
            manifest=manifest,
            resolved_domains=resolve_demo_domains(manifest),
            settings=_settings(),
        )
    assert changed.value.issue.code == "source_digest_mismatch"

    invalid = b"not-a-zip"
    with pytest.raises(HypothesisGenerationError) as missing:
        await generate_code_driven_hypothesis(
            capacity=_capacity(source_archive_digest(invalid)),
            source_archive=invalid,
            runtime_profile_digest=_digest("runtime"),
            priorities=[_priority()],
            manifest=manifest,
            resolved_domains=resolve_demo_domains(manifest),
            settings=_settings(),
        )
    assert missing.value.issue.code == "source_archive_invalid"


@pytest.mark.asyncio
async def test_generation_uses_read_only_tools_then_revalidates_single_candidate() -> None:
    archive = _archive()
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/storage.py",
                                                    "startLine": 1,
                                                    "endLine": 3,
                                                }
                                            ),
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "hypotheses": [
                                        {
                                            "statement": (
                                                "Reducing block scheduler work may move the "
                                                "measured capacity frontier."
                                            ),
                                            "candidate_parameters": {
                                                "system.storage-scheduler": "none"
                                            },
                                            "source_evidence": [
                                                {
                                                    "file": "src/storage.py",
                                                    "start_line": 2,
                                                    "end_line": 2,
                                                    "claim": (
                                                        "The workload commits storage requests "
                                                        "synchronously."
                                                    ),
                                                    "symbol": "submit",
                                                }
                                            ],
                                        }
                                    ]
                                }
                            )
                        },
                    }
                ]
            },
        )

    manifest = build_demo_manifest()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await generate_code_driven_hypothesis(
            capacity=_capacity(source_archive_digest(archive)),
            source_archive=archive,
            runtime_profile_digest=_digest("runtime"),
            priorities=[_priority()],
            manifest=manifest,
            resolved_domains=resolve_demo_domains(manifest),
            settings=_settings(),
            client=client,
        )

    assert requests[0]["tool_choice"] == "required"
    assert requests[0]["temperature"] == 0
    assert result.hypothesis.candidate_parameters == {"system.storage-scheduler": "none"}
    assert result.hypothesis.state == HypothesisState.SUPPORTED_HYPOTHESIS
    assert [item["tool"] for item in result.trace] == ["read_file"]
