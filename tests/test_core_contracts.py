from __future__ import annotations

import json

import pytest
from looper_core.canonical import canonical_digest, canonical_json
from looper_core.contracts import (
    ClientLoadAccounting,
    Direction,
    ExperimentMode,
    ExperimentSpec,
    ObjectiveSpec,
    ScenarioBenchmarkSpec,
    SelectionDesign,
    TargetBindingSpec,
)
from looper_core.manifest import ManifestError, load_and_validate_manifest, validate_document
from looper_core.state import (
    AttemptStatus,
    ExperimentStatus,
    InvalidTransition,
    require_attempt_transition,
    require_experiment_transition,
)


def test_canonical_json_is_stable_and_rejects_nan() -> None:
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert canonical_digest({"a": 1}) == canonical_digest(json.loads('{"a": 1}'))
    with pytest.raises(ValueError):
        canonical_json({"bad": float("nan")})


def test_demo_manifest_validates() -> None:
    manifest, digest = load_and_validate_manifest(
        __import__("pathlib").Path("benchmarks/demo/benchmark.yaml")
    )
    assert manifest["metadata"]["id"] == "looper.demo.compression"
    assert digest.startswith("sha256:")


@pytest.mark.parametrize(
    "path",
    [
        "benchmarks/benchbase-smallbank/benchmark.yaml",
        "benchmarks/dcperf-mediawiki/benchmark.yaml",
    ],
)
def test_scenario_manifests_validate(path: str) -> None:
    manifest, digest = load_and_validate_manifest(__import__("pathlib").Path(path))
    scenario = ScenarioBenchmarkSpec.model_validate(manifest["spec"]["scenario"])
    assert scenario.primary_metric in manifest["spec"]["metrics"]
    assert digest.startswith("sha256:")
    assert manifest["spec"]["x-extensions"]["executionStatus"] == "stage0-adapter-only"


def test_client_load_accounting_closes_the_request_chain() -> None:
    accounting = ClientLoadAccounting(
        schemaVersion="v1alpha1",
        plannedOfferedTps=2000,
        measurementSeconds=60,
        offeredRequests=120000,
        startedRequests=119900,
        completedRequests=119800,
        timeoutRequests=100,
        rateLimiterLagRatio=0.001,
        clientHeadroomRatio=0.25,
    )
    assert accounting.completed_requests + accounting.timeout_requests == 119900
    validate_document(
        accounting.model_dump(mode="json", by_alias=True),
        "client-load-accounting.schema.json",
    )
    with pytest.raises(ValueError, match="account for every started request"):
        accounting.model_validate(
            {
                **accounting.model_dump(mode="json", by_alias=True),
                "completedRequests": 119700,
            }
        )
    with pytest.raises(ValueError, match="must be finite"):
        accounting.model_validate(
            {
                **accounting.model_dump(mode="json", by_alias=True),
                "plannedOfferedTps": float("inf"),
            }
        )


def test_selection_contract_binds_every_target() -> None:
    manifest, _ = load_and_validate_manifest(
        __import__("pathlib").Path("benchmarks/benchbase-smallbank/benchmark.yaml")
    )
    spec = ExperimentSpec(
        mode=ExperimentMode.SELECTION,
        benchmark_id=manifest["metadata"]["id"],
        benchmark_version=manifest["metadata"]["version"],
        target_ids=["s9", "sa9"],
        workload_ids=["smallbank-postgres-serializable"],
        objectives=[
            ObjectiveSpec(
                metric="committed_tps",
                unit="transactions/second",
                direction=Direction.MAXIMIZE,
            )
        ],
        scenario=ScenarioBenchmarkSpec.model_validate(manifest["spec"]["scenario"]),
        selection=SelectionDesign(
            target_bindings=[
                TargetBindingSpec(
                    target_id="s9", variant_id="s9", label="S9", placement_pair_id="wave-1"
                ),
                TargetBindingSpec(
                    target_id="sa9", variant_id="sa9", label="SA9", placement_pair_id="wave-1"
                ),
            ]
        ),
    )
    assert spec.mode == ExperimentMode.SELECTION
    with pytest.raises(ValueError, match="exactly match"):
        spec.model_copy(update={"target_ids": ["s9"]}).model_validate(
            {**spec.model_dump(), "target_ids": ["s9"]}
        )


def test_manifest_rejects_shell_commands() -> None:
    with pytest.raises(ManifestError):
        validate_document(
            {
                "apiVersion": "looper.dev/v1alpha1",
                "kind": "Benchmark",
                "metadata": {
                    "id": "bad.shell",
                    "name": "bad",
                    "version": "1",
                    "license": "INTERNAL",
                },
                "spec": {
                    "trust": "trusted",
                    "parameters": {},
                    "workloads": [{"id": "one", "name": "one"}],
                    "runtime": {
                        "type": "local-process",
                        "commands": {"run": "python bench.py"},
                    },
                    "metrics": {
                        "score": {"unit": "score", "direction": "maximize", "kind": "sample"}
                    },
                    "outputs": {"maxBytes": 1024, "artifacts": []},
                },
            },
            "benchmark-manifest.schema.json",
        )


def test_state_transitions_are_explicit() -> None:
    require_experiment_transition(ExperimentStatus.DRAFT, ExperimentStatus.QUEUED)
    require_attempt_transition(AttemptStatus.QUEUED, AttemptStatus.LEASED)
    with pytest.raises(InvalidTransition):
        require_experiment_transition(ExperimentStatus.COMPLETED, ExperimentStatus.RUNNING)
    with pytest.raises(InvalidTransition):
        require_attempt_transition(AttemptStatus.SUCCEEDED, AttemptStatus.RUNNING)
