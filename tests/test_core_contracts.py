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
        "docs/examples/benchmark-single-node.yaml",
        "docs/examples/benchmark-multi-node.yaml",
    ],
)
def test_integration_templates_validate(path: str) -> None:
    manifest, digest = load_and_validate_manifest(__import__("pathlib").Path(path))
    infrastructure = manifest["spec"]["infrastructure"]

    assert infrastructure["primaryNodeGroup"]
    assert infrastructure["nodeGroups"]
    assert digest.startswith("sha256:")


@pytest.mark.parametrize(
    ("path", "execution_status"),
    [
        ("benchmarks/benchbase-smallbank/benchmark.yaml", "stage0-adapter-only"),
        ("benchmarks/dcperf-mediawiki/benchmark.yaml", "executable"),
    ],
)
def test_scenario_manifests_validate(path: str, execution_status: str) -> None:
    manifest, digest = load_and_validate_manifest(__import__("pathlib").Path(path))
    scenario = ScenarioBenchmarkSpec.model_validate(manifest["spec"]["scenario"])
    assert scenario.primary_metric in manifest["spec"]["metrics"]
    assert digest.startswith("sha256:")
    assert manifest["spec"]["x-extensions"]["executionStatus"] == execution_status


@pytest.mark.parametrize(
    "path",
    [
        "benchmarks/sysbench/benchmark.yaml",
        "benchmarks/dcperf-mediawiki/benchmark.yaml",
        "benchmarks/phoronix-phpbench/benchmark.yaml",
    ],
)
def test_builtin_diagnostic_contracts_validate(path: str) -> None:
    manifest, _ = load_and_validate_manifest(__import__("pathlib").Path(path))
    contract = manifest["spec"]["x-extensions"]["diagnosticRecommendations"]
    assert contract["enabled"] is True
    assert contract["policy"]["modeMinimumSamples"] >= 8
    assert contract["rules"]


def test_diagnostic_contract_rejects_invalid_threshold_order() -> None:
    manifest, _ = load_and_validate_manifest(
        __import__("pathlib").Path("benchmarks/sysbench/benchmark.yaml")
    )
    invalid = json.loads(json.dumps(manifest))
    policy = invalid["spec"]["x-extensions"]["diagnosticRecommendations"]["policy"]
    policy["cvStable"] = policy["cvUnstable"]
    with pytest.raises(ManifestError, match="cvStable must be lower"):
        validate_document(invalid, "benchmark-manifest.schema.json")


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


def _minimal_presentation_manifest(presentation: dict | None = None) -> dict:
    metric = {"unit": "score", "direction": "maximize", "kind": "sample"}
    if presentation is not None:
        metric["presentation"] = presentation
    return {
        "apiVersion": "looper.dev/v1alpha1",
        "kind": "Benchmark",
        "metadata": {
            "id": "test.semantics",
            "name": "test",
            "version": "1",
            "license": "INTERNAL",
        },
        "spec": {
            "trust": "trusted",
            "parameters": {},
            "workloads": [{"id": "one", "name": "one"}],
            "runtime": {
                "type": "local-process",
                "commands": {"run": {"argv": ["python", "bench.py"], "timeoutSeconds": 60}},
            },
            "metrics": {"score": metric},
            "outputs": {"maxBytes": 1024, "artifacts": []},
        },
    }


@pytest.mark.parametrize(
    "path",
    [
        "benchmarks/demo/benchmark.yaml",
        "benchmarks/sysbench/benchmark.yaml",
        "benchmarks/benchbase-smallbank/benchmark.yaml",
        "benchmarks/dcperf-mediawiki/benchmark.yaml",
        "benchmarks/config-driven-fixture/benchmark.yaml",
    ],
)
def test_all_benchmarks_declare_valid_presentation(path: str) -> None:
    manifest, digest = load_and_validate_manifest(__import__("pathlib").Path(path))
    assert digest.startswith("sha256:")
    # Every benchmark relying on presentation must produce resolvable metric
    # definitions that carry the controlled presentation vocabulary.
    metrics = manifest["spec"]["metrics"]
    assert metrics
    for _name, declaration in metrics.items():
        if "presentation" not in declaration:
            continue
        presentation = declaration["presentation"]
        for role in presentation.get("roles", []):
            assert role in {
                "primary_outcome",
                "hard_gate",
                "guardrail",
                "cost_efficiency",
                "stability",
                "diagnostic",
                "context",
            }
        if "defaultVisibility" in presentation:
            assert presentation["defaultVisibility"] in {"summary", "detail", "expert", "hidden"}
        if "displayPrecision" in presentation:
            assert presentation["displayPrecision"] >= 0


def test_presentation_rejects_unknown_role() -> None:
    with pytest.raises(ManifestError):
        validate_document(
            _minimal_presentation_manifest({"roles": ["primary_outcome", "bogus"]}),
            "benchmark-manifest.schema.json",
        )


def test_presentation_rejects_duplicate_roles() -> None:
    with pytest.raises(ManifestError):
        validate_document(
            _minimal_presentation_manifest({"roles": ["hard_gate", "hard_gate"]}),
            "benchmark-manifest.schema.json",
        )


def test_presentation_rejects_negative_display_precision() -> None:
    with pytest.raises(ManifestError):
        validate_document(
            _minimal_presentation_manifest({"displayPrecision": -1}),
            "benchmark-manifest.schema.json",
        )


def test_metric_without_presentation_loads() -> None:
    document = _minimal_presentation_manifest()
    validate_document(document, "benchmark-manifest.schema.json")
    assert "presentation" not in document["spec"]["metrics"]["score"]
