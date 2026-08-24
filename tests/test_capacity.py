from __future__ import annotations

import io
import zipfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from looper_api import capacity
from looper_api.capacity import (
    BuildEvidence,
    BuildPlan,
    CapacityBuildRepairRequest,
    CapacityDraft,
    CapacityDraftUpdate,
    CapacityError,
    CapacityStartRequest,
    create_capacity_study,
    draft_constraints,
    ensure_capacity_benchmark,
    preflight_capacity_study,
    repair_capacity_build_plan,
    start_capacity_study,
    update_capacity_study,
)
from looper_api.config import Settings
from looper_api.models import SourceDiscoveryRecord, TargetRecord
from looper_api.source_archive_store import EncryptedSourceArchiveStore, SourceArchiveError
from looper_api.source_discovery import (
    CONTRACT_VERSION,
    purge_expired_archives,
    replace_retained_archive,
    source_archive_digest,
)
from looper_api.worker_protocol import WorkerRegister
from looper_api.worker_service import claim_attempt, register_worker
from looper_core.canonical import utc_now
from sqlalchemy.orm import sessionmaker


def _archive(entries: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, deepseek_api_key="test-key", deepseek_model="test")


def _source(session: object, settings: Settings) -> tuple[SourceDiscoveryRecord, bytes]:
    payload = _archive(
        {
            "app.py": (
                "from fastapi import FastAPI\n"
                "app = FastAPI()\n"
                "@app.post('/orders')\n"
                "def create_order(): return {'id': 1}\n"
            )
        }
    )
    now = utc_now()
    record = SourceDiscoveryRecord(
        id="discovery_capacity_test",
        archive_name="orders.zip",
        source_digest=source_archive_digest(payload),
        status="completed",
        provider="deepseek",
        model="test",
        file_manifest_json=[{"path": "app.py", "bytes": 120, "lines": 4}],
        excluded_files_json=[],
        contract_json={
            "apiVersion": CONTRACT_VERSION,
            "spec": {
                "interfaces": [
                    {
                        "id": "create-order",
                        "method": "POST",
                        "path": "/orders",
                        "summary": "创建订单",
                        "sideEffect": "write",
                        "responses": [{"statusCode": 200}],
                        "evidence": [{"file": "app.py", "startLine": 3, "endLine": 4}],
                    }
                ]
            },
        },
        trace_json=[],
        error_code=None,
        error_message=None,
        archive_retained_until=None,
        archive_deleted_at=None,
        archive_delete_reason=None,
        created_at=now,
        completed_at=now,
    )
    session.add(record)  # type: ignore[attr-defined]
    session.flush()  # type: ignore[attr-defined]
    record.archive_retained_until = EncryptedSourceArchiveStore(settings).save(record.id, payload)
    return record, payload


def _build_plan() -> BuildPlan:
    return BuildPlan(
        dockerfile="FROM python:3.12-slim\nCOPY . /app\nWORKDIR /app\n",
        compose=(
            "services:\n"
            "  app:\n"
            "    build: .\n"
            "    command: python app.py\n"
            "    ports: ['8000:8000']\n"
        ),
        startCommand="python app.py",
        healthPath="/health",
        servicePort=8000,
        evidence=[BuildEvidence(file="app.py", startLine=1, endLine=4)],
    )


def _target(target_id: str, *, runnable: bool) -> TargetRecord:
    now = utc_now()
    return TargetRecord(
        id=target_id,
        name=target_id,
        provider="external",
        status="online" if runnable else "inventory-only",
        capabilities_json=["python", "local-process"] if runnable else [],
        inventory_json={"instance_type": "test.small"},
        fingerprint_json={},
        snapshot_digest=f"sha256:{target_id}",
        runnable=runnable,
        lifecycle_status="active",
        last_inventory_seen_at=now,
        inventory_missing_since=None,
        inventory_miss_count=0,
        archived_at=None,
        archive_reason=None,
        created_at=now,
        updated_at=now,
    )


def test_source_archive_is_encrypted_expires_and_requires_same_digest(
    db_session: object, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    source, payload = _source(db_session, settings)
    store = EncryptedSourceArchiveStore(settings)

    encrypted = store.path(source.id).read_bytes()
    assert payload not in encrypted
    assert b"FastAPI" not in encrypted
    assert store.load(source.id) == payload

    different = _archive({"app.py": "print('different')\n"})
    with pytest.raises(Exception, match="does not match"):
        replace_retained_archive(db_session, source, different, settings)  # type: ignore[arg-type]

    source.archive_retained_until = utc_now() - timedelta(seconds=1)
    assert purge_expired_archives(db_session, settings) == 1  # type: ignore[arg-type]
    assert not store.exists(source.id)
    assert source.archive_delete_reason == "retention_expired"


@pytest.mark.asyncio
async def test_capacity_draft_constraints_revision_preflight_and_partial_start(
    db_session: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = db_session
    settings = _settings(tmp_path)
    source, _payload = _source(session, settings)

    async def build(*_args: object, **_kwargs: object) -> BuildPlan:
        return _build_plan()

    monkeypatch.setattr(capacity, "run_build_plan_harness", build)
    study = await create_capacity_study(session, source, settings, name="订单容量")
    draft = CapacityDraft.model_validate(study.draft_json)
    assert draft.scenario.steps[0].path == "/orders"
    assert (
        next(item for item in draft_constraints(draft) if item["code"] == "scenario.reset")[
            "status"
        ]
        == "fail"
    )

    draft.build.approved = True
    draft.scenario.reset_strategy = "compose-recreate"
    draft.targets.sut_ids = ["sut-pass", "sut-fail"]
    draft.targets.internal_load_generator_id = "loadgen"
    draft.targets.external_load_generator_id = "loadgen"
    draft.targets.internal_base_urls = {
        "sut-pass": "http://10.0.0.1:8000",
        "sut-fail": "http://10.0.0.2:8000",
    }
    draft.targets.external_base_urls = {
        "sut-pass": "http://203.0.113.1:8000",
        "sut-fail": "http://203.0.113.2:8000",
    }
    for target in (
        _target("sut-pass", runnable=False),
        _target("sut-fail", runnable=False),
        _target("loadgen", runnable=True),
    ):
        session.add(target)
    update_capacity_study(
        study,
        CapacityDraftUpdate(expectedRevision=1, currentStep=4, draft=draft),
    )
    assert study.revision == 2
    with pytest.raises(CapacityError, match="another session"):
        update_capacity_study(
            study,
            CapacityDraftUpdate(expectedRevision=1, currentStep=4, draft=draft),
        )

    monkeypatch.setattr(
        capacity,
        "_ssh_docker_check",
        lambda target_id, _settings: (
            (True, "Docker Compose ready")
            if target_id == "sut-pass"
            else (False, "Docker unavailable")
        ),
    )
    result = preflight_capacity_study(session, study, settings)
    assert result["failedSutIds"] == ["sut-fail"]
    assert result["generatorFailures"] == []
    with pytest.raises(CapacityError, match="exactly match"):
        start_capacity_study(
            session,
            study,
            CapacityStartRequest(expectedRevision=2, excludedTargetIds=[]),
            settings,
        )
    started = start_capacity_study(
        session,
        study,
        CapacityStartRequest(
            expectedRevision=2,
            excludedTargetIds=["sut-fail"],
            acknowledgePartial=True,
        ),
        settings,
    )
    assert started.status == "queued"
    assert started.execution_json["activeTargetIds"] == ["sut-pass"]
    assert started.execution_json["excludedTargetIds"] == ["sut-fail"]
    assert started.execution_json["budget"]["maxSeconds"] == draft.budget.max_seconds
    assert started.execution_json["costControl"] == {
        "currency": "CNY",
        "limit": draft.budget.cost_cap,
        "scope": "incremental capacity-run charges",
        "pricingStatus": "not-applicable-existing-resources",
        "estimatedIncrementalAmount": 0.0,
        "detail": "本次容量运行只使用已登记服务器，不创建云资源；既有服务器租金不计入增量费用。",
    }

    benchmark = ensure_capacity_benchmark(session, started, settings)
    request = capacity._network_experiment_request(session, started, benchmark, "internal")
    assert request.spec.selection is not None
    assert request.spec.selection.load_generator_target_id == "loadgen"
    assert request.spec.budget.max_attempts == draft.budget.max_attempts // 2
    assert request.spec.budget.max_candidates == 1
    assert request.spec.budget.wall_time_seconds * 2 <= draft.budget.max_seconds

    experiment = capacity.create_capacity_experiment(session, started, settings, "internal")
    live = capacity.capacity_view(session, started, settings)["execution"]["liveMatrix"]
    assert live[0]["targetId"] == "sut-pass"
    assert live[0]["currentLoad"] == pytest.approx(draft.budget.reference_rps * 0.5)
    wrong = register_worker(
        session,
        settings,
        WorkerRegister(
            workerId="worker-on-sut",
            name="worker on SUT",
            token=settings.local_worker_token,
            capabilities=["python", "local-process"],
            targetIds=["sut-pass"],
            fingerprint={},
        ),
    )
    assert claim_attempt(session, settings, wrong) is None
    load_generator = register_worker(
        session,
        settings,
        WorkerRegister(
            workerId="worker-on-loadgen",
            name="worker on load generator",
            token=settings.local_worker_token,
            capabilities=["python", "local-process"],
            targetIds=["loadgen"],
            fingerprint={},
        ),
    )
    claim = claim_attempt(session, settings, load_generator)
    assert claim is not None
    assert claim["envelope"]["experimentId"] == experiment.id
    assert claim["envelope"]["extensions"]["executionTargetId"] == "loadgen"
    assert claim["envelope"]["extensions"]["targetBinding"]["target_id"] == "sut-pass"
    cancelled = capacity.cancel_capacity_study(session, started)
    assert cancelled.status == "cancelling"
    assert experiment.status == "cancelled"


def test_build_plan_rejects_global_or_privileged_compose_names() -> None:
    plan = _build_plan()
    plan.compose += "    container_name: shared-app\n    privileged: true\n"
    failures = capacity._build_plan_constraints(plan)
    assert any("global container name" in item for item in failures)
    assert any("privileged" in item for item in failures)


def test_build_plan_script_detects_zip_root_and_orders_database_migrations(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    workspace = capacity.SourceWorkspace.from_zip(
        _archive(
            {
                "poll-service-master/go.mod": "module example.test/polls\n",
                "poll-service-master/migrations/v1/001_options.sql": (
                    "CREATE TABLE options (id int PRIMARY KEY, poll_id int "
                    "REFERENCES polls(id));\n"
                ),
                "poll-service-master/migrations/v1/002_polls.sql": (
                    "CREATE TABLE polls (id int PRIMARY KEY, user_id int "
                    "REFERENCES users(id));\n"
                ),
                "poll-service-master/migrations/v1/003_users.sql": (
                    "CREATE TABLE users (id int PRIMARY KEY);\n"
                ),
                "poll-service-master/migrations/v1/004_votes.sql": (
                    "CREATE TABLE votes (id int PRIMARY KEY, user_id int REFERENCES users(id), "
                    "poll_id int REFERENCES polls(id), option_id int REFERENCES options(id));\n"
                ),
            }
        ),
        settings,
    )
    plan = BuildPlan(
        dockerfile="FROM golang:1.24\nCOPY . /src\n",
        compose=(
            "services:\n"
            "  db:\n"
            "    image: postgres:17\n"
            "    volumes: ['./migrations:/docker-entrypoint-initdb.d']\n"
            "  app:\n"
            "    build: .\n"
            "    ports: ['8080:8080']\n"
        ),
        startCommand="./poll-service",
        healthPath="/swagger/index.html",
        servicePort=8080,
        unresolved=["A dedicated database-aware readiness endpoint was not found"],
    )

    result = capacity.run_build_plan_script(workspace, plan)

    assert result.source_root == "poll-service-master"
    assert result.unresolved == []
    assert result.advisories == [
        "A dedicated database-aware readiness endpoint was not found"
    ]
    assert result.ordered_migrations == [
        "migrations/v1/003_users.sql",
        "migrations/v1/002_polls.sql",
        "migrations/v1/001_options.sql",
        "migrations/v1/004_votes.sql",
    ]
    assert "./.looper-capacity-migrations:/docker-entrypoint-initdb.d:ro" in result.compose
    checks = {item.id: item for item in result.checks}
    assert checks["source-root"].status == "fixed"
    assert checks["migration-order"].status == "fixed"


def test_deploy_uses_detected_root_generated_dockerfile_and_ordered_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    archive = _archive(
        {
            "repo/go.mod": "module example.test/polls\n",
            "repo/migrations/001_child.sql": (
                "CREATE TABLE child (id int, parent_id int REFERENCES parent(id));\n"
            ),
            "repo/migrations/002_parent.sql": "CREATE TABLE parent (id int PRIMARY KEY);\n",
        }
    )
    workspace = capacity.SourceWorkspace.from_zip(archive, settings)
    build = capacity.run_build_plan_script(
        workspace,
        BuildPlan(
            dockerfile="FROM golang:1.24\nCOPY . /src\n",
            compose=(
                "services:\n"
                "  db:\n"
                "    image: postgres:17\n"
                "    volumes: ['./migrations:/docker-entrypoint-initdb.d']\n"
                "  app:\n"
                "    build:\n"
                "      context: .\n"
                "      dockerfile: Dockerfile.generated\n"
                "    ports: ['8080:8080']\n"
            ),
            startCommand="./poll-service",
            healthPath="/health",
            servicePort=8080,
        ),
    )
    draft = CapacityDraft(build=build, scenario={"steps": []})
    record = SimpleNamespace(
        id="capacity_deploy_path_test",
        draft_json=draft.model_dump(mode="json", by_alias=True),
    )
    written: dict[str, bytes | str] = {}
    commands: list[str] = []

    class RemoteFile:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> RemoteFile:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def write(self, value: bytes | str) -> None:
            written[self.path] = value

    class Sftp:
        def file(self, path: str, _mode: str) -> RemoteFile:
            return RemoteFile(path)

        def close(self) -> None:
            return None

    class Client:
        def open_sftp(self) -> Sftp:
            return Sftp()

        def close(self) -> None:
            return None

    def remote_run(_client: object, command: str, *, timeout: int = 900) -> str:
        del timeout
        commands.append(command)
        return "/home/looper" if command.startswith("printf") else ""

    monkeypatch.setattr(
        capacity.EncryptedSshCredentialStore,
        "load",
        lambda _store, _target_id: object(),
    )
    monkeypatch.setattr(capacity, "open_ssh_client", lambda _request: Client())
    monkeypatch.setattr(capacity, "_remote_run", remote_run)

    result = capacity.deploy_capacity_target(
        record,  # type: ignore[arg-type]
        "sut-1",
        archive,
        settings,
    )

    project_root = "/home/looper/.looper-capacity/capacity_deploy_path_test/source/repo"
    assert f"{project_root}/Dockerfile.generated" in written
    assert f"{project_root}/compose.capacity.yaml" in written
    assert any(
        "migrations/002_parent.sql" in command
        and ".looper-capacity-migrations/0001_002_parent.sql" in command
        for command in commands
    )
    config_index = next(
        index for index, command in enumerate(commands) if " config -q" in command
    )
    build_index = next(
        index for index, command in enumerate(commands) if command.endswith(" build")
    )
    up_index = next(
        index for index, command in enumerate(commands) if " up -d --wait" in command
    )
    assert config_index < build_index < up_index
    assert result["status"] == "healthy"


@pytest.mark.asyncio
async def test_build_plan_repair_runs_script_before_agent_and_records_evidence(
    db_session: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    source, _payload = _source(db_session, settings)
    initial = _build_plan()
    initial.unresolved = ["health endpoint does not prove database readiness"]
    calls: list[BuildPlan | None] = []

    async def build(
        *_args: object, previous_plan: BuildPlan | None = None, **_kwargs: object
    ) -> BuildPlan:
        calls.append(previous_plan)
        return initial

    monkeypatch.setattr(capacity, "run_build_plan_harness", build)
    study = await create_capacity_study(db_session, source, settings)  # type: ignore[arg-type]
    stored = CapacityDraft.model_validate(study.draft_json)
    stored.build.unresolved = ["health endpoint does not prove database readiness"]
    stored.build.advisories = []
    stored.build.checks = []
    study.draft_json = stored.model_dump(mode="json", by_alias=True)
    bypass = CapacityDraft.model_validate(study.draft_json)
    bypass.build.unresolved = []
    bypass.build.approved = True
    with pytest.raises(CapacityError, match="scripted build validation"):
        update_capacity_study(
            study,
            CapacityDraftUpdate(expectedRevision=1, currentStep=0, draft=bypass),
        )
    result = await repair_capacity_build_plan(
        db_session,  # type: ignore[arg-type]
        study,
        CapacityBuildRepairRequest(expectedRevision=1),
        settings,
    )

    assert result.revision == 2
    assert calls == [None]
    repaired = CapacityDraft.model_validate(result.draft_json).build
    assert repaired.unresolved == []
    assert repaired.advisories == ["health endpoint does not prove database readiness"]
    assert repaired.checks
    validation = result.execution_json["buildValidations"][-1]
    assert validation["mode"] == "script"
    assert validation["agentUsed"] is False
    assert validation["revisionBefore"] == 1
    assert validation["revisionAfter"] == 2
    assert validation["unresolvedBefore"] == [
        "health endpoint does not prove database readiness"
    ]
    assert validation["blockers"] == []
    assert validation["provider"] == "deterministic-script"
    with pytest.raises(CapacityError, match="another session"):
        await repair_capacity_build_plan(
            db_session,  # type: ignore[arg-type]
            result,
            CapacityBuildRepairRequest(expectedRevision=1),
            settings,
        )


@pytest.mark.asyncio
async def test_remote_build_failure_cleans_and_returns_to_build_step(
    db_session: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from looper_api import database

    settings = _settings(tmp_path)
    source, _payload = _source(db_session, settings)

    async def build(*_args: object, **_kwargs: object) -> BuildPlan:
        return _build_plan()

    monkeypatch.setattr(capacity, "run_build_plan_harness", build)
    study = await create_capacity_study(db_session, source, settings)  # type: ignore[arg-type]
    build_validations = study.execution_json["buildValidations"]
    study.status = "queued"
    study.execution_json = {
        "phases": [],
        "activeTargetIds": ["sut-1"],
        "buildValidations": build_validations,
    }
    db_session.commit()  # type: ignore[attr-defined]
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)  # type: ignore[attr-defined]
    monkeypatch.setattr(database, "SessionLocal", factory)

    def fail_deploy(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise CapacityError(
            "scripted build validation failed: service did not become healthy",
            code="capacity_build_validation_failed",
        )

    monkeypatch.setattr(capacity, "deploy_capacity_target", fail_deploy)
    monkeypatch.setattr(
        capacity,
        "cleanup_capacity_target",
        lambda _record, target_id, _settings: {
            "targetId": target_id,
            "status": "clean",
            "cleanedAt": utc_now().isoformat(),
        },
    )

    capacity._prepare_capacity_job(study.id, settings)

    db_session.expire_all()  # type: ignore[attr-defined]
    failed = db_session.get(type(study), study.id)  # type: ignore[attr-defined]
    assert failed.status == "draft"
    assert failed.current_step == 0
    assert failed.revision == 2
    assert failed.error_code == "capacity_build_validation_failed"
    assert failed.execution_json["cleanup"][0]["status"] == "clean"
    assert failed.execution_json["buildValidations"] == build_validations
    failed_draft = CapacityDraft.model_validate(failed.draft_json)
    assert failed_draft.build.approved is False
    assert failed_draft.build.unresolved[0].startswith("远程脚本验证失败")


@pytest.mark.asyncio
async def test_expired_source_blocks_a_saved_draft_before_build(
    db_session: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    source, _payload = _source(db_session, settings)

    async def build(*_args: object, **_kwargs: object) -> BuildPlan:
        return _build_plan()

    monkeypatch.setattr(capacity, "run_build_plan_harness", build)
    study = await create_capacity_study(db_session, source, settings)  # type: ignore[arg-type]
    source.archive_retained_until = utc_now() - timedelta(seconds=1)
    with pytest.raises(SourceArchiveError, match="retention expired"):
        start_capacity_study(
            db_session,  # type: ignore[arg-type]
            study,
            CapacityStartRequest(expectedRevision=1),
            settings,
        )
