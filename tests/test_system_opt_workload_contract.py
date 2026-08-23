"""M3 workload contract schema tests (SO-D020 boundary, workload-tuning.md D0)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from looper_core.system_opt.workload import (
    LoadCommandIdentity,
    WorkloadContract,
    WorkloadContractError,
    load_argv_digest,
    parse_workload_contract_yaml,
    same_load,
)

EXAMPLE = (
    Path(__file__).parents[1]
    / "examples"
    / "system-optimizer"
    / "stress-ng-workload-contract.yaml"
)
EXAMPLE_ARGV = ["stress-ng", "--cpu", "4", "--timeout", "120s", "--metrics-brief"]


def _load_command(argv_digest: str = "sha256:" + "a" * 64) -> LoadCommandIdentity:
    return LoadCommandIdentity(
        tool="stress-ng",
        argv_digest=argv_digest,
        declared_duration_seconds=120,
        description="test-side owned load command",
    )


def _payload(**overrides) -> dict:
    payload: dict = {
        "schema_version": "looper.workload-contract/v1alpha1",
        "workload_id": "stress-ng-standin-test",
        "load_provider": "external-test",
        "load_command": _load_command().model_dump(mode="json"),
        "o0_metrics": [
            {
                "metric_id": "stress-ng.bogo-ops",
                "unit": "bogo-ops/s",
                "direction": "maximize",
                "aggregation": "mean",
                "source": "stress-ng output",
            },
            {
                "metric_id": "stress-ng.failed-ops",
                "unit": "ops",
                "direction": "minimize",
                "aggregation": "maximum",
                "source": "stress-ng output",
            },
        ],
        "objective": {
            "primary_metric_id": "stress-ng.bogo-ops",
            "scale": 1.0,
            "mde": 0.01,
        },
        "slos": [],
        "correctness_gates": [
            {
                "metric_id": "stress-ng.failed-ops",
                "comparator": "at-most",
                "bound": 0,
                "unit": "ops",
            }
        ],
        "phases": [
            {
                "phase_id": "steady",
                "purpose": "sustained load",
                "o0_metric_ids": ["stress-ng.bogo-ops", "stress-ng.failed-ops"],
            }
        ],
        "limitations": "test fixture contract",
    }
    payload.update(overrides)
    return payload


def test_contract_round_trips_with_deterministic_digest():
    contract = WorkloadContract.model_validate(_payload())
    rebuilt = WorkloadContract.model_validate(
        yaml.safe_load(yaml.safe_dump(_payload()))
    )

    assert contract.digest == rebuilt.digest
    assert contract.digest.startswith("sha256:")
    assert contract.load_provider == "external-test"


def test_only_external_test_provider_is_accepted():
    payload = _payload(load_provider="optimizer")

    with pytest.raises(WorkloadContractError, match="load_provider"):
        parse_workload_contract_yaml(yaml.safe_dump(payload))


def test_undeclared_metric_references_are_rejected():
    with pytest.raises(ValidationError, match="objective primary"):
        WorkloadContract.model_validate(
            _payload(
                objective={
                    "primary_metric_id": "not.declared",
                    "scale": 1.0,
                    "mde": 0.0,
                }
            )
        )
    with pytest.raises(ValidationError, match="correctness gate metric"):
        WorkloadContract.model_validate(
            _payload(
                correctness_gates=[
                    {
                        "metric_id": "also.not.declared",
                        "comparator": "at-most",
                        "bound": 0,
                        "unit": "ops",
                    }
                ]
            )
        )
    with pytest.raises(ValidationError, match="undeclared o0 metrics"):
        WorkloadContract.model_validate(
            _payload(
                phases=[
                    {
                        "phase_id": "steady",
                        "purpose": "sustained load",
                        "o0_metric_ids": ["missing.metric"],
                    }
                ]
            )
        )


def test_duplicate_metric_and_phase_ids_are_rejected():
    duplicated = _payload()
    duplicated["o0_metrics"] = duplicated["o0_metrics"] + [
        dict(duplicated["o0_metrics"][0])
    ]
    with pytest.raises(ValidationError, match="unique"):
        WorkloadContract.model_validate(duplicated)


def test_correctness_gates_cannot_be_empty():
    with pytest.raises(ValidationError, match="correctness_gates"):
        WorkloadContract.model_validate(_payload(correctness_gates=[]))


def test_load_identity_digest_is_argv_reproducible_and_exact_match():
    left = LoadCommandIdentity(
        tool="stress-ng",
        argv_digest=load_argv_digest(EXAMPLE_ARGV),
        declared_duration_seconds=120,
        description="window one",
    )
    same_again = left.model_copy(update={"description": "re-worded prose"})
    different_argv = LoadCommandIdentity(
        tool="stress-ng",
        argv_digest=load_argv_digest([*EXAMPLE_ARGV, "--cpu", "8"]),
        declared_duration_seconds=120,
        description="window one",
    )

    assert same_load(left, same_again)
    assert not same_load(left, different_argv)
    assert left.identity_digest != different_argv.identity_digest


def test_example_contract_is_valid_and_argv_digest_is_bound():
    contract = parse_workload_contract_yaml(EXAMPLE.read_text(encoding="utf-8"))

    assert contract.workload_id == "stress-ng-cpu-standin-v1"
    assert contract.load_command.argv_digest == load_argv_digest(EXAMPLE_ARGV)
    assert contract.objective.primary_metric_id == "stress-ng.bogo-ops"
    assert contract.phases[0].phase_id == "steady"
