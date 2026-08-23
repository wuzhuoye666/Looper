from __future__ import annotations

from looper_api.benchmark_runs import BenchmarkSmokeRunRequest, create_benchmark_smoke_run
from looper_api.models import AttemptRecord
from looper_core.state import ExperimentStatus
from sqlalchemy import select


def test_configuration_driven_package_creates_one_attempt_smoke_run(db_session) -> None:
    experiment = create_benchmark_smoke_run(
        db_session,
        "looper.fixture.config-driven",
        "1.1.0",
        BenchmarkSmokeRunRequest(parameters={"scale": 2}),
    )
    attempts = list(
        db_session.scalars(
            select(AttemptRecord).where(AttemptRecord.experiment_id == experiment.id)
        )
    )

    assert experiment.status == ExperimentStatus.QUEUED
    assert len(attempts) == 1
    assert experiment.spec_json["baseline_parameters"] == {"scale": 2}
    assert experiment.spec_json["workload_ids"] == ["fixture-small"]
