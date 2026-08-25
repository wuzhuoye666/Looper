from __future__ import annotations

import json

import pytest
from looper_api.models import (
    ArtifactLinkRecord,
    ArtifactRecord,
    AttemptRecord,
    CandidateRecord,
    EvaluationRecord,
    ExperimentRecord,
)
from looper_api.system_optimization_inputs import (
    AUTHORIZATION_PROFILE_SCHEMA,
    RUNTIME_PROFILE_SCHEMA,
    SystemOptimizationInputError,
    load_authorization_profile,
    load_runtime_diagnostic_profile,
)
from looper_core.canonical import canonical_digest, utc_now
from looper_core.cas import FileSystemCAS
from looper_core.system_opt.demo import build_demo_manifest, resolve_demo_domains
from looper_core.system_opt.scoring import DiagnosticPriority


def _register_json(db_session, cas: FileSystemCAS, payload: dict[str, object]) -> str:
    stored = cas.put_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    db_session.add(
        ArtifactRecord(
            digest=stored.digest,
            size=stored.size,
            verified=True,
            created_at=utc_now(),
        )
    )
    db_session.flush()
    return stored.digest


def _attempt(db_session, *, experiment_id: str = "capacity-experiment") -> AttemptRecord:
    now = utc_now()
    experiment = ExperimentRecord(
        id=experiment_id,
        project_id="default",
        name="capacity evidence fixture",
        description="",
        status="completed",
        spec_json={},
        spec_digest=canonical_digest({}),
        revision=1,
        optimizer_state_json={},
        created_at=now,
        updated_at=now,
        started_at=now,
        finished_at=now,
    )
    candidate = CandidateRecord(
        id=f"candidate-{experiment_id}",
        experiment_id=experiment_id,
        sequence=1,
        role="baseline",
        parameters_json={},
        config_digest=canonical_digest({"candidate": experiment_id}),
        status="completed",
        infeasible_reason=None,
        created_at=now,
        completed_at=now,
    )
    evaluation = EvaluationRecord(
        id=f"evaluation-{experiment_id}",
        experiment_id=experiment_id,
        candidate_id=candidate.id,
        workload_id="business-iteration",
        target_id="local",
        target_snapshot_digest=canonical_digest({"target": "local"}),
        target_snapshot_json={},
        status="completed",
        created_at=now,
        completed_at=now,
    )
    attempt = AttemptRecord(
        id=f"attempt-{experiment_id}",
        experiment_id=experiment_id,
        evaluation_id=evaluation.id,
        selection_load_point_id=None,
        repeat_index=0,
        retry_index=0,
        queue_sequence=1,
        status="succeeded",
        fencing_token=1,
        worker_id=None,
        lease_expires_at=None,
        idempotency_key=f"attempt-{experiment_id}",
        envelope_json=None,
        envelope_digest=None,
        exit_code=0,
        error_message=None,
        phase=None,
        phase_detail=None,
        created_at=now,
        leased_at=now,
        started_at=now,
        completed_at=now,
    )
    # These models intentionally do not expose ORM relationships. Flush each FK
    # layer explicitly so the fixture exercises the same database constraints as
    # production instead of relying on SQLAlchemy object-graph ordering.
    db_session.add(experiment)
    db_session.flush()
    db_session.add(candidate)
    db_session.flush()
    db_session.add(evaluation)
    db_session.flush()
    db_session.add(attempt)
    db_session.flush()
    return attempt


def _link_profile(db_session, attempt: AttemptRecord, digest: str, *, name: str) -> None:
    db_session.add(
        ArtifactLinkRecord(
            id=f"link-{name}",
            attempt_id=attempt.id,
            digest=digest,
            role="profile",
            name=name,
            media_type="application/json",
            producer="test-runtime-profiler",
            created_at=utc_now(),
        )
    )
    db_session.flush()


def _runtime_payload(context_digest: str, component: str = "storage") -> dict[str, object]:
    priority = DiagnosticPriority(
        metric_id=f"{component}.pressure",
        component=component,
        pressure=0.8,
        adverse_change=0.4,
        persistence=0.9,
        confidence=0.95,
        pareto_rank=1,
    )
    return {
        "schemaVersion": RUNTIME_PROFILE_SCHEMA,
        "capacityContextDigest": context_digest,
        "targetId": "local",
        "priorities": [priority.model_dump(mode="json")],
        "measurementSummary": {"source": "attempt profile artifact"},
    }


def test_authorization_profile_requires_verified_target_bound_dynamic_domains(
    db_session, tmp_path
) -> None:
    cas = FileSystemCAS(tmp_path / "cas")
    manifest = build_demo_manifest()
    payload = {
        "schemaVersion": AUTHORIZATION_PROFILE_SCHEMA,
        "targetId": "local",
        "manifest": manifest.model_dump(mode="json", by_alias=True),
        "resolvedDomains": [
            domain.model_dump(mode="json", by_alias=True)
            for domain in resolve_demo_domains(manifest).values()
        ],
        "reason": "operator-approved target capability evidence",
    }
    digest = _register_json(db_session, cas, payload)

    profile = load_authorization_profile(db_session, cas, digest, target_id="local")

    assert profile.target_id == "local"
    assert profile.domain_mapping()["system.storage-scheduler"].item_id == (
        "storage-scheduler"
    )
    with pytest.raises(SystemOptimizationInputError) as mismatched:
        load_authorization_profile(db_session, cas, digest, target_id="other-target")
    assert mismatched.value.code == "authorization_profile_target_mismatch"

    invalid_payload = dict(payload)
    invalid_payload["resolvedDomains"] = [
        {
            **payload["resolvedDomains"][-1],
            "choices": ["not-supported-by-manifest"],
        }
    ]
    invalid_digest = _register_json(db_session, cas, invalid_payload)
    with pytest.raises(SystemOptimizationInputError) as invalid:
        load_authorization_profile(db_session, cas, invalid_digest, target_id="local")
    assert invalid.value.code == "authorization_profile_invalid"


def test_runtime_profile_missing_is_an_explicit_recoverable_stop(db_session, tmp_path) -> None:
    cas = FileSystemCAS(tmp_path / "cas")
    _attempt(db_session)

    with pytest.raises(SystemOptimizationInputError) as raised:
        load_runtime_diagnostic_profile(
            db_session,
            cas,
            experiment_id="capacity-experiment",
            capacity_context_digest=canonical_digest({"context": "baseline"}),
            target_id="local",
        )

    assert raised.value.code == "runtime_profile_missing"
    assert raised.value.recoverable is True


def test_runtime_profile_must_match_experiment_context_target_and_storage_route(
    db_session, tmp_path
) -> None:
    cas = FileSystemCAS(tmp_path / "cas")
    attempt = _attempt(db_session)
    context_digest = canonical_digest({"context": "baseline"})
    digest = _register_json(db_session, cas, _runtime_payload(context_digest))
    _link_profile(db_session, attempt, digest, name="runtime-profile.json")

    resolved_digest, profile = load_runtime_diagnostic_profile(
        db_session,
        cas,
        experiment_id="capacity-experiment",
        capacity_context_digest=context_digest,
        target_id="local",
    )

    assert resolved_digest == digest
    assert [priority.component for priority in profile.priorities] == ["storage"]
    with pytest.raises(SystemOptimizationInputError) as changed_context:
        load_runtime_diagnostic_profile(
            db_session,
            cas,
            experiment_id="capacity-experiment",
            capacity_context_digest=canonical_digest({"context": "changed"}),
            target_id="local",
        )
    assert changed_context.value.code == "runtime_profile_missing"


def test_runtime_profile_without_measured_storage_priority_is_not_a_bottleneck_claim(
    db_session, tmp_path
) -> None:
    cas = FileSystemCAS(tmp_path / "cas")
    attempt = _attempt(db_session)
    context_digest = canonical_digest({"context": "baseline"})
    digest = _register_json(db_session, cas, _runtime_payload(context_digest, "cpu"))
    _link_profile(db_session, attempt, digest, name="cpu-profile.json")

    with pytest.raises(SystemOptimizationInputError) as raised:
        load_runtime_diagnostic_profile(
            db_session,
            cas,
            experiment_id="capacity-experiment",
            capacity_context_digest=context_digest,
            target_id="local",
        )

    assert raised.value.code == "runtime_profile_missing"
    assert raised.value.recoverable is True
