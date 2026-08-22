from __future__ import annotations

import json
from pathlib import Path

from looper_core.action_loop import (
    ActionDecision,
    ActionMeasurement,
    JsonFileAction,
    VerificationPolicy,
    execute_verified_action,
)


def _validate_state(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"value"}:
        raise ValueError("state requires one value")
    selected = int(value["value"])
    if not 0 <= selected <= 10:
        raise ValueError("value is outside the action allowlist")
    return {"value": selected}


def _action(tmp_path: Path) -> JsonFileAction:
    return JsonFileAction("test.action", tmp_path / "active.json", _validate_state, {"value": 1})


def _policy(minimum: float = 0.05) -> VerificationPolicy:
    return VerificationPolicy(
        repeats=3,
        minimum_improvement_ratio=minimum,
        maximum_secondary_regression_ratio=0.10,
        confidence_level=0.95,
        bootstrap_resamples=500,
        random_seed=7,
    )


def test_verified_action_accepts_and_keeps_candidate(tmp_path: Path) -> None:
    action = _action(tmp_path)

    def measure(state: object, repeat: int, phase: str) -> ActionMeasurement:
        del repeat, phase
        selected = state.value["value"]  # type: ignore[attr-defined]
        return ActionMeasurement(
            primary=100.0 if selected == 1 else 120.0,
            secondary=0.50 if selected == 1 else 0.52,
            gates={"correctness": True},
        )

    result = execute_verified_action(action, {"value": 2}, measure, _policy())

    assert result["decision"] == ActionDecision.ACCEPTED
    assert result["rollbackVerified"] is None
    assert action.readback().value == {"value": 2}
    assert result["finalState"]["value"] == {"value": 2}
    assert any(item["event"] == "action.accepted" for item in result["auditTrail"])


def test_verified_action_rolls_back_a_regression(tmp_path: Path) -> None:
    action = _action(tmp_path)

    def measure(state: object, repeat: int, phase: str) -> ActionMeasurement:
        del repeat, phase
        selected = state.value["value"]  # type: ignore[attr-defined]
        return ActionMeasurement(
            primary=100.0 if selected == 1 else 80.0,
            secondary=0.50,
            gates={"correctness": True},
        )

    result = execute_verified_action(action, {"value": 2}, measure, _policy())

    assert result["decision"] == ActionDecision.ROLLED_BACK
    assert result["rollbackVerified"] is True
    assert action.readback().value == {"value": 1}
    assert result["finalState"]["digest"] == result["baselineState"]["digest"]


def test_verified_action_fails_closed_when_evidence_is_inconclusive(tmp_path: Path) -> None:
    action = _action(tmp_path)
    candidate_values = [90.0, 105.0, 120.0]

    def measure(state: object, repeat: int, phase: str) -> ActionMeasurement:
        del phase
        selected = state.value["value"]  # type: ignore[attr-defined]
        return ActionMeasurement(
            primary=100.0 if selected == 1 else candidate_values[repeat],
            secondary=0.50,
            gates={"correctness": True},
        )

    result = execute_verified_action(action, {"value": 2}, measure, _policy())

    assert result["decision"] == ActionDecision.INCONCLUSIVE
    assert result["rollbackVerified"] is True
    assert action.readback().value == {"value": 1}


def test_verified_action_rolls_back_after_candidate_measurement_failure(tmp_path: Path) -> None:
    action = _action(tmp_path)

    def measure(state: object, repeat: int, phase: str) -> ActionMeasurement:
        selected = state.value["value"]  # type: ignore[attr-defined]
        if selected == 2 and repeat == 1:
            raise RuntimeError("injected benchmark failure")
        return ActionMeasurement(
            primary=100.0,
            secondary=0.50,
            gates={"correctness": True},
            evidence={"phase": phase},
        )

    result = execute_verified_action(action, {"value": 2}, measure, _policy())

    assert result["decision"] == ActionDecision.ROLLED_BACK
    assert result["rollbackVerified"] is True
    assert "injected benchmark failure" in result["reason"]
    assert action.readback().value == {"value": 1}


def test_json_file_action_writes_a_readable_durable_state(tmp_path: Path) -> None:
    action = _action(tmp_path)
    action.apply({"value": 3})

    assert json.loads((tmp_path / "active.json").read_text(encoding="utf-8")) == {"value": 3}
    assert not list(tmp_path.glob("*.tmp"))
