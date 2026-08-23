from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
import yaml
from looper_api.cli import _decoupled_pressure_measure
from looper_core.system_opt.collector import COLLECTION_BUNDLE_MEDIA_TYPE
from looper_core.system_opt.pressure import (
    StandardPressureProtocol,
    parse_standard_pressure_protocol_yaml,
)

try:  # additive PKG-B contract; absent until the decoupling lands on this branch
    from looper_core.system_opt.pressure import PhasedPressureCollectionAdapter  # noqa: F401

    _HAS_COLLECTION_CONTRACT = True
except ImportError:
    _HAS_COLLECTION_CONTRACT = False

requires_collection_contract = pytest.mark.skipif(
    not _HAS_COLLECTION_CONTRACT,
    reason="pressure collection contract has not landed yet (PKG-B)",
)

_EXAMPLES = Path(__file__).parents[1] / "examples" / "system-optimizer"


def test_legacy_pressure_protocol_keeps_the_legacy_adapter_route() -> None:
    protocol = parse_standard_pressure_protocol_yaml(
        (_EXAMPLES / "cpu-pressure-calibration-protocol.yaml").read_text(encoding="utf-8")
    )

    assert getattr(protocol, "collection", None) is None
    assert (
        _decoupled_pressure_measure(
            protocol,
            None,
            target_id="target-1",
            collection_enabled=True,
        )
        is None
    )


def _collection_payload(collector_id: str) -> dict[str, Any]:
    return {
        "schema_version": "looper.standard-pressure-protocol/v1alpha1",
        "id": "cpu-decoupled-cli-v1",
        "component": "cpu",
        "target_scope": "one explicit fixture target",
        "limitation": "fixture evidence only",
        "required_executables": ["prepare", "warmup", "measure", "verify", "cleanup"],
        "input_identity": {"policy_id": "fixture-policy"},
        "metric_ids": ["cpu.score", "cpu.success"],
        "gate_metric_ids": ["cpu.success"],
        "stability": {
            "metric_id": "cpu.score",
            "statistic": "cv",
            "enforcement": "report-only",
            "acceptance_limit": None,
            "minimum_repeats": 3,
            "maximum_repeats": 3,
            "source": "fixture-only contract",
        },
        "collection": {
            "collector_id": collector_id,
            "requested_metrics": ["cpu.score", "cpu.success"],
            "artifact_requirements": [
                {
                    "artifact_id": "cpu-tool-bundle",
                    "media_type": COLLECTION_BUNDLE_MEDIA_TYPE,
                }
            ],
            "interval_seconds": 0.25,
            "scope": {},
            "workload_source": "fixture controlled CPU workload",
        },
        "phases": [
            {
                "id": "prepare",
                "kind": "prepare",
                "command": {"argv": ["prepare", "{repeats}"], "timeout_seconds": 2},
                "declared_duration_seconds": 0,
                "purpose": "freeze fixture input",
            },
            {
                "id": "warmup",
                "kind": "warmup",
                "command": {"argv": ["warmup", "{repeats}"], "timeout_seconds": 3},
                "declared_duration_seconds": 1,
                "purpose": "discard fixture warmup",
            },
            {
                "id": "measure",
                "kind": "measure",
                "command": {"argv": ["measure", "{repeats}"], "timeout_seconds": 4},
                "declared_duration_seconds": 2,
                "purpose": "emit digest-bound pressure artifacts only",
            },
            {
                "id": "verify",
                "kind": "verify",
                "command": {"argv": ["verify", "{repeats}"], "timeout_seconds": 2},
                "declared_duration_seconds": 0,
                "purpose": "verify fixture artifacts",
            },
            {
                "id": "cleanup",
                "kind": "cleanup",
                "command": {"argv": ["cleanup", "{repeats}"], "timeout_seconds": 2},
                "declared_duration_seconds": 0,
                "purpose": "remove fixture workload",
            },
        ],
    }


class _FakeWindowedCollector:
    collector_id = "fixture.windowed-cpu"
    collector_version = "1.0"

    def begin_collection(self, plan: Any) -> Any:
        return SimpleNamespace(plan=plan)


@requires_collection_contract
def test_disabled_collection_switch_fails_closed_for_measurement_entry_points() -> None:
    protocol = StandardPressureProtocol.model_validate(
        _collection_payload("fixture.windowed-cpu")
    )

    with pytest.raises(typer.BadParameter, match="require collection to be enabled"):
        _decoupled_pressure_measure(
            protocol,
            None,
            target_id="target-1",
            collection_enabled=False,
            collector=_FakeWindowedCollector(),
        )


@requires_collection_contract
def test_unavailable_collector_identity_fails_closed() -> None:
    protocol = StandardPressureProtocol.model_validate(
        _collection_payload("fixture.windowed-cpu")
    )

    with pytest.raises(typer.BadParameter, match="available to this CLI"):
        _decoupled_pressure_measure(
            protocol,
            None,
            target_id="target-1",
            collection_enabled=True,
        )


@requires_collection_contract
def test_builtin_windowed_collector_wires_the_decoupled_route() -> None:
    """Since PKG-B the builtin collector implements begin_collection, so a
    builtin-named collection contract wires the decoupled route; the measure
    callable is only constructed here, never invoked (running the real
    pressure phases belongs to integration runs)."""

    protocol = StandardPressureProtocol.model_validate(
        _collection_payload("looper.builtin-linux-guest")
    )

    measure = _decoupled_pressure_measure(
        protocol,
        None,
        target_id="target-1",
        collection_enabled=True,
    )

    assert callable(measure)


@requires_collection_contract
def test_windowed_collector_routes_and_unwraps_the_envelope(monkeypatch: Any) -> None:
    protocol = StandardPressureProtocol.model_validate(
        _collection_payload("fixture.windowed-cpu")
    )
    captured: dict[str, Any] = {}

    class _StubAdapter:
        def __init__(
            self,
            inner_protocol: Any,
            runner: Any,
            *,
            collector: Any,
            target_id: str,
            environment_digest: str,
            collection_enabled: bool,
        ) -> None:
            captured.update(
                collector=collector,
                target_id=target_id,
                environment_digest=environment_digest,
                collection_enabled=collection_enabled,
            )

        def __call__(self, repeats: int) -> Any:
            return SimpleNamespace(
                envelope=SimpleNamespace(measurement_batch="BOUND-BATCH")
            )

    monkeypatch.setattr(
        "looper_core.system_opt.pressure.PhasedPressureCollectionAdapter",
        _StubAdapter,
    )

    measure = _decoupled_pressure_measure(
        protocol,
        None,
        target_id="target-1",
        collection_enabled=True,
        collector=_FakeWindowedCollector(),
    )

    assert measure is not None
    assert measure(3) == "BOUND-BATCH"
    assert captured["target_id"] == "target-1"
    assert captured["collection_enabled"] is True
    assert captured["environment_digest"].startswith("sha256:")
    assert isinstance(captured["collector"], _FakeWindowedCollector)


@requires_collection_contract
def test_missing_envelope_on_an_enabled_run_fails_closed(monkeypatch: Any) -> None:
    protocol = StandardPressureProtocol.model_validate(
        _collection_payload("fixture.windowed-cpu")
    )

    class _NoEnvelopeAdapter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __call__(self, repeats: int) -> Any:
            return SimpleNamespace(envelope=None)

    monkeypatch.setattr(
        "looper_core.system_opt.pressure.PhasedPressureCollectionAdapter",
        _NoEnvelopeAdapter,
    )

    measure = _decoupled_pressure_measure(
        protocol,
        None,
        target_id="target-1",
        collection_enabled=True,
        collector=_FakeWindowedCollector(),
    )

    assert measure is not None
    with pytest.raises(RuntimeError, match="no measurement envelope"):
        measure(3)


@requires_collection_contract
def test_cli_yaml_payload_round_trips_through_the_protocol_contract(tmp_path: Path) -> None:
    payload = _collection_payload("fixture.windowed-cpu")
    protocol_path = tmp_path / "collection-protocol.yaml"
    protocol_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    parsed = parse_standard_pressure_protocol_yaml(
        protocol_path.read_text(encoding="utf-8")
    )

    assert parsed.collection is not None
    assert parsed.collection.collector_id == "fixture.windowed-cpu"
    assert parsed.digest.startswith("sha256:")
