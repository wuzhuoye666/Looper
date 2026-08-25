from __future__ import annotations

from pathlib import Path

from looper_api.app import (
    claim_worker_endpoint,
    register_worker_endpoint,
    start_worker_attempt_endpoint,
)
from looper_api.config import Settings
from looper_api.models import AttemptRecord, Base, BenchmarkRecord, TargetRecord
from looper_api.scheduler import create_demo_request, create_experiment, start_experiment
from looper_api.seed import seed_system
from looper_api.worker_protocol import AttemptStart, WorkerClaim, WorkerRegister
from looper_core.canonical import canonical_digest, utc_now
from looper_core.manifest import load_and_validate_manifest
from looper_core.state import AttemptStatus
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def test_claim_is_committed_before_start_response(tmp_path: Path) -> None:
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'transactions.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'transactions.db').as_posix()}",
        local_worker_token="secret",
    )
    with factory() as setup:
        seed_system(setup)
        manifest_path = Path("benchmarks/demo/benchmark.yaml").resolve()
        manifest, digest = load_and_validate_manifest(manifest_path)
        metadata = manifest["metadata"]
        setup.add(BenchmarkRecord(
            key=f"{metadata['id']}@{metadata['version']}",
            benchmark_id=metadata["id"],
            version=metadata["version"],
            name=metadata["name"],
            description=metadata.get("description", ""),
            license=metadata["license"],
            manifest_digest=digest,
            manifest_json=manifest,
            manifest_path=str(manifest_path),
            package_digest=None,
            trusted=True,
            installed_at=utc_now(),
        ))
        fingerprint = {
            "system": "Linux",
            "architecture": "x86_64",
            "logical_cpu_count": 8,
            "memory_gib": 16,
        }
        setup.add(TargetRecord(
            id="local",
            name="Transaction test target",
            provider="fixture",
            status="available",
            capabilities_json=["python", "local-process", "linux", "x86_64"],
            inventory_json={"source": "test"},
            fingerprint_json=fingerprint,
            snapshot_digest=canonical_digest({"fingerprint": fingerprint}),
            runnable=True,
            lifecycle_status="active",
            created_at=utc_now(),
            updated_at=utc_now(),
        ))
        setup.flush()
        experiment = create_experiment(setup, create_demo_request())
        start_experiment(setup, experiment)
        setup.commit()

    with factory() as claim_session:
        register_worker_endpoint(
            WorkerRegister(
                workerId="worker-transaction",
                name="transaction worker",
                token="secret",
                capabilities=["python", "local-process"],
                fingerprint={},
            ),
            claim_session,
            settings,
        )
        response = claim_worker_endpoint(
            WorkerClaim(workerId="worker-transaction"),
            claim_session,
            settings,
            "secret",
        )
        claim = response["claim"]
        assert claim is not None

    with factory() as start_session:
        persisted = start_session.get(AttemptRecord, claim["attemptId"])
        assert persisted and persisted.status == AttemptStatus.LEASED
        start_worker_attempt_endpoint(
            persisted.id,
            AttemptStart(
                workerId="worker-transaction",
                fencingToken=claim["fencingToken"],
                envelope=claim["envelope"],
            ),
            start_session,
            settings,
            "secret",
        )

    with factory() as verify:
        persisted = verify.get(AttemptRecord, claim["attemptId"])
        assert persisted and persisted.status == AttemptStatus.RUNNING

    engine.dispose()
