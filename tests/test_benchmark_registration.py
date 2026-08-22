from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from looper_api.benchmark_registration import (
    BenchmarkRegistrationDraft,
    BenchmarkRegistrationRegister,
    BenchmarkRegistrationUpdate,
    RegistrationError,
    create_registration,
    draft_from_manifest_bytes,
    evaluate_registration_constraints,
    register_benchmark,
    registration_ready,
    update_registration,
)
from looper_api.models import BenchmarkRecord, EventRecord
from looper_api.serialization import benchmark_view
from sqlalchemy import select


def _draft() -> BenchmarkRegistrationDraft:
    path = Path(__file__).parents[1] / "benchmarks" / "benchbase-smallbank" / "benchmark.yaml"
    manifest = deepcopy(yaml.safe_load(path.read_text(encoding="utf-8")))
    manifest["metadata"].update(
        {
            "id": "tc.smallbank.audit",
            "name": "TC SmallBank audit",
            "version": "0.1.0",
        }
    )
    source = manifest["metadata"]["source"]
    return BenchmarkRegistrationDraft(
        name=manifest["metadata"]["name"],
        benchmarkId=manifest["metadata"]["id"],
        version=manifest["metadata"]["version"],
        sourceUrl=source["url"],
        sourceRevision=source["commit"],
        license=manifest["metadata"]["license"],
        category="database",
        decisionQuestion=manifest["spec"]["scenario"]["decision_question"],
        primaryMetric="committed_tps",
        primaryUnit="transactions/second",
        correctnessContract="p99 latency and abort rate are hard gates",
        runtimeType="container",
        executionStatus="stage0-adapter-only",
        image="",
        minimumSamples=1,
        repeats=3,
        hasReference=True,
        retainsRawEvidence=True,
        crossEnvironmentAudit=True,
        manifest=manifest,
    )


def test_server_constraints_require_complete_traceable_contract() -> None:
    constraints, digest = evaluate_registration_constraints(_draft())

    assert registration_ready(constraints)
    assert digest and digest.startswith("sha256:")
    assert all(item["detail"] for item in constraints)


def test_yaml_configuration_import_prefills_manifest_facts() -> None:
    path = Path(__file__).parents[1] / "benchmarks" / "benchbase-smallbank" / "benchmark.yaml"
    draft = draft_from_manifest_bytes(path.read_bytes(), filename=path.name)

    assert draft.benchmark_id == "benchbase.smallbank.postgres"
    assert draft.runtime_type == "container"
    assert draft.primary_metric == "committed_tps"
    assert draft.manifest is not None
    assert draft.correctness_contract == ""
    assert draft.cross_environment_audit is False


def test_non_scenario_adapter_uses_declared_metric_and_required_checks() -> None:
    path = (
        Path(__file__).parents[1]
        / "benchmarks"
        / "config-driven-fixture"
        / "benchmark.yaml"
    )
    draft = draft_from_manifest_bytes(path.read_bytes(), filename=path.name).model_copy(
        update={
            "decision_question": "Does the configured adapter execute correctly?",
            "correctness_contract": "native-result-valid must pass",
            "has_reference": True,
            "cross_environment_audit": True,
        }
    )

    constraints, _ = evaluate_registration_constraints(draft)
    by_code = {item["code"]: item for item in constraints}

    assert draft.primary_metric == "fixture_score"
    assert by_code["contract.scenario-semantics"]["status"] == "pass"
    assert by_code["contract.hard-gates"]["status"] == "pass"


def test_registration_lifecycle_is_optimistically_locked_and_immutable(db_session) -> None:
    record = create_registration(db_session, _draft())
    assert record.revision == 1
    assert record.status == "draft"

    changed = _draft().model_copy(update={"category": "cpu-iaas"})
    record = update_registration(
        db_session,
        record.id,
        BenchmarkRegistrationUpdate(expectedRevision=1, draft=changed),
    )
    assert record.revision == 2

    with pytest.raises(RegistrationError, match="revision conflict"):
        update_registration(
            db_session,
            record.id,
            BenchmarkRegistrationUpdate(expectedRevision=1, draft=changed),
        )

    record = register_benchmark(
        db_session,
        record.id,
        BenchmarkRegistrationRegister(expectedRevision=2),
    )
    benchmark = db_session.get(BenchmarkRecord, "tc.smallbank.audit@0.1.0")

    assert record.status == "registered"
    assert benchmark is not None
    assert benchmark.trusted is False
    assert benchmark.manifest_path is None
    assert benchmark_view(benchmark, record)["runnable"] is False
    assert benchmark_view(benchmark, record)["auditStatus"] == "registered-not-admitted"
    events = list(
        db_session.scalars(
            select(EventRecord)
            .where(EventRecord.entity_id == record.id)
            .order_by(EventRecord.created_at)
        )
    )
    assert [item.event_type for item in events] == [
        "benchmark.registration.created",
        "benchmark.registration.updated",
        "benchmark.registration.registered",
    ]
    assert events[-1].payload_json["manifestDigest"] == benchmark.manifest_digest
    assert events[-1].payload_json["runnable"] is False

    with pytest.raises(RegistrationError, match="immutable"):
        update_registration(
            db_session,
            record.id,
            BenchmarkRegistrationUpdate(expectedRevision=3, draft=changed),
        )


def test_remote_registration_cannot_install_executable_bundle(db_session) -> None:
    draft = _draft()
    assert draft.manifest is not None
    draft.execution_status = "executable"
    draft.image = "registry.example/bench@sha256:" + "a" * 64
    draft.manifest["spec"]["runtime"]["image"] = draft.image
    draft.manifest["spec"]["x-extensions"]["executionStatus"] = "executable"
    record = create_registration(db_session, draft)

    with pytest.raises(RegistrationError) as captured:
        register_benchmark(
            db_session,
            record.id,
            BenchmarkRegistrationRegister(expectedRevision=1),
        )

    assert captured.value.code == "registration_constraints_failed"
    assert any(
        item["code"] == "execution.install-boundary" and item["status"] == "fail"
        for item in captured.value.constraints or []
    )
    assert db_session.scalar(
        select(BenchmarkRecord).where(BenchmarkRecord.benchmark_id == draft.benchmark_id)
    ) is None


def test_generic_container_adapter_can_register_without_backend_plugin(db_session) -> None:
    draft = _draft()
    assert draft.manifest is not None
    draft.execution_status = "executable"
    draft.image = "registry.example/bench@sha256:" + "b" * 64
    draft.manifest["spec"]["runtime"]["image"] = draft.image
    draft.manifest["spec"]["runtime"]["commands"]["normalize"] = {
        "argv": ["benchmark-normalize", "--output", "{output}"],
        "timeoutSeconds": 30,
    }
    draft.manifest["spec"]["adapter"] = {
        "protocol": "looper-adapter/v1",
        "executionModel": "database",
        "primaryMetric": draft.primary_metric,
        "requiredChecks": ["native-correctness"],
        "inputs": [],
        "canonicalOutputs": {"metrics": "metrics.jsonl", "result": "result.json"},
    }
    draft.manifest["spec"]["x-extensions"]["executionStatus"] = "executable"
    record = create_registration(db_session, draft)

    assert record.constraints_json
    assert registration_ready(record.constraints_json)
    record = register_benchmark(
        db_session,
        record.id,
        BenchmarkRegistrationRegister(expectedRevision=1),
    )
    benchmark = db_session.get(BenchmarkRecord, record.benchmark_key)
    assert benchmark is not None
    assert benchmark_view(benchmark, record)["runnable"] is True
    events = list(
        db_session.scalars(
            select(EventRecord)
            .where(EventRecord.entity_id == record.id)
            .order_by(EventRecord.created_at)
        )
    )
    assert events[-1].payload_json["runnable"] is True
