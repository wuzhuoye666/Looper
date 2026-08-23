from __future__ import annotations

from datetime import UTC, datetime

import pytest
from looper_core.system_opt.collector import (
    CollectedMetric,
    ComponentMetricSnapshot,
    MetricAvailability,
)
from looper_core.system_opt.component import CandidateSuggestion, ComponentOptimizer
from looper_core.system_opt.component.mapping import (
    CandidateRule,
    ConditionOperator,
    EvidenceCondition,
    StrategyFormulaMapping,
)
from looper_core.system_opt.demo import (
    SyntheticMeasurementAdapter,
    build_demo_manifest,
    build_demo_policy,
    resolve_demo_domains,
)
from looper_core.system_opt.engine import EngineLoopConfig, EngineStopReason, run_engine_loop
from looper_core.system_opt.executor import ConfigSnapshot, OperationStatus
from looper_core.system_opt.executor.simulated import SimulatedBackend
from looper_core.system_opt.negative_cache import NegativeCache
from looper_core.system_opt.policy import OptimizationMode
from looper_core.system_opt.rollback import RestorationStatus
from looper_core.system_opt.tuning import SystemOptimizationEngine

ENV = "sha256:" + "a" * 64
PROTOCOL = "sha256:" + "b" * 64
FORMULAS = {"F-COMPOSITION": "v1"}
FIXED_AT = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


def _fixture(mapping=None, *, generator="grid"):
    manifest = build_demo_manifest()
    initial = {item.id: item.default for item in manifest.items}
    backend = SimulatedBackend(initial, target_id="composition-test")
    policy = build_demo_policy(OptimizationMode.GENERAL)
    policy.authorized_components = ["cpu"]
    policy.search.generator = generator
    policy.search.max_candidates = 2
    policy.search.max_attempts = 4
    policy.search.no_improvement_limit = 3
    policy.search.target_improvement = None
    engine = SystemOptimizationEngine(
        policy, manifest, resolve_demo_domains(manifest), backend
    )
    optimizer = ComponentOptimizer(engine, mapping)
    defaults = {item.parameter_id: item.default for item in manifest.items}
    config = EngineLoopConfig(
        environment_digest=ENV,
        formula_versions=FORMULAS,
        pressure_protocol_digests={"cpu": PROTOCOL},
        max_rounds=2,
        max_pool_size=64,
    )
    measure = SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL)
    return optimizer, manifest, backend, defaults, config, measure


def _snapshot(value: float = 0.9) -> ComponentMetricSnapshot:
    return ComponentMetricSnapshot(
        component="cpu",
        target_id="composition-test",
        environment_digest=ENV,
        collected_at=FIXED_AT,
        metrics={
            "cpu.load": CollectedMetric(
                name="cpu.load",
                unit="ratio",
                value=value,
                availability=MetricAvailability.READABLE,
                source="test-evidence",
            )
        },
        counting_basis="composition regression fixture",
    )


def _strategy_mapping(parameters=None) -> StrategyFormulaMapping:
    return StrategyFormulaMapping(
        [
            CandidateRule(
                rule_id="cpu-high-load",
                when=[
                    EvidenceCondition(
                        metric_id="cpu.load",
                        operator=ConditionOperator.GT,
                        threshold=0.5,
                    )
                ],
                suggest_parameters=parameters or {"system.cpu-governor": "performance"},
                rationale="high load maps to the authorized performance governor",
                formula_id="F-COMPOSITION/v1",
                priority=1,
            )
        ]
    )


def _run(optimizer, defaults, config, measure, **kwargs):
    return run_engine_loop(
        [optimizer],
        baseline_parameters={"cpu": defaults},
        measures={"cpu": measure},
        negative_cache=NegativeCache(),
        config=config,
        fencing_token=7,
        **kwargs,
    )


class TestFormulaEvidenceAndSelectionIdentity:
    def test_real_mapping_without_evidence_fails_closed(self):
        optimizer, _, _, defaults, config, measure = _fixture(_strategy_mapping())
        with pytest.raises(ValueError, match="at least one evidence source"):
            _run(optimizer, defaults, config, measure)

    def test_snapshot_drives_formula_candidate_into_execution(self, monkeypatch):
        optimizer, _, _, defaults, config, measure = _fixture(_strategy_mapping())
        monkeypatch.setattr(optimizer, "candidate_pool", lambda: [])
        result = _run(
            optimizer,
            defaults,
            config,
            measure,
            component_snapshots={"cpu": _snapshot()},
        )
        record = result.rounds[0]
        assert record.selected_parameters == {"system.cpu-governor": "performance"}
        assert record.formula_rejections == []
        assert len(record.verdicts) == 1

    def test_scheduler_selection_equals_random_engine_evaluation(self, monkeypatch):
        optimizer, _, _, defaults, config, measure = _fixture(generator="random")
        selected = {"system.cpu-governor": "performance"}
        monkeypatch.setattr(optimizer, "candidate_pool", lambda: [selected])
        original_run = optimizer.run
        evaluated = []

        def capture_run(**kwargs):
            report = original_run(**kwargs)
            evaluated.extend(candidate.parameters for candidate in report.candidates)
            return report

        monkeypatch.setattr(optimizer, "run", capture_run)
        result = _run(optimizer, defaults, config, measure)
        record = result.rounds[0]
        assert record.selected_parameters == selected
        assert evaluated == [selected]
        assert len(record.verdicts) == 1

    def test_out_of_domain_formula_is_recorded_and_never_executed(self, monkeypatch):
        class MixedMapping:
            def suggest(self, snapshot, baseline):
                return [
                    CandidateSuggestion(
                        parameters={"system.unknown": "x"},
                        rationale="invalid fixture",
                        formula_id="F-BAD/v1",
                    ),
                    CandidateSuggestion(
                        parameters={"system.cpu-governor": "performance"},
                        rationale="valid fixture",
                        formula_id="F-GOOD/v1",
                    ),
                ]

        optimizer, _, _, defaults, config, measure = _fixture(MixedMapping())
        monkeypatch.setattr(optimizer, "candidate_pool", lambda: [])
        result = _run(optimizer, defaults, config, measure)
        record = result.rounds[0]
        assert record.selected_parameters == {"system.cpu-governor": "performance"}
        assert [item.rule_id for item in record.formula_rejections] == ["F-BAD/v1"]
        assert "not in the resolved search space" in record.formula_rejections[0].reason
        assert result.formula_rejections["cpu"] == record.formula_rejections

    def test_rule_miss_rejection_is_preserved_in_engine_result(self, monkeypatch):
        optimizer, _, _, defaults, config, measure = _fixture(_strategy_mapping())
        monkeypatch.setattr(optimizer, "candidate_pool", lambda: [])
        result = _run(
            optimizer,
            defaults,
            config,
            measure,
            component_snapshots={"cpu": _snapshot(0.1)},
        )
        assert result.stop_reason is EngineStopReason.NO_ACTIONABLE_CANDIDATES
        rejection = result.formula_rejections["cpu"][0]
        assert rejection.rule_id == "cpu-high-load"
        assert "fails gt 0.5" in rejection.reason

    def test_final_pool_budget_counts_formula_suggestions(self, monkeypatch):
        class TwoSuggestionMapping:
            def suggest(self, snapshot, baseline):
                return [
                    CandidateSuggestion(
                        parameters={"system.cpu-governor": "performance"},
                        rationale="candidate one",
                        formula_id="F-ONE/v1",
                    ),
                    CandidateSuggestion(
                        parameters={"system.cpu-governor": "powersave"},
                        rationale="candidate two",
                        formula_id="F-TWO/v1",
                    ),
                ]

        optimizer, _, _, defaults, config, measure = _fixture(TwoSuggestionMapping())
        monkeypatch.setattr(optimizer, "candidate_pool", lambda: [])
        config.max_pool_size = 1
        with pytest.raises(ValueError, match="final candidate pool 2.*max_pool_size=1"):
            _run(optimizer, defaults, config, measure)


class TestPhaseRestorationEndingGate:
    @staticmethod
    def _changed_snapshot(baseline: ConfigSnapshot, *, incomplete: bool) -> ConfigSnapshot:
        entries = {name: entry.model_copy(deep=True) for name, entry in baseline.entries.items()}
        name = next(iter(entries))
        if incomplete:
            entries[name] = entries[name].model_copy(
                update={"status": OperationStatus.FAILED, "message": "readback failed"}
            )
        else:
            entries[name] = entries[name].model_copy(update={"value": "unexpected"})
        return ConfigSnapshot(target_id=baseline.target_id, entries=entries)

    @pytest.mark.parametrize(
        ("incomplete", "expected"),
        [(False, RestorationStatus.MISMATCH), (True, RestorationStatus.INCOMPLETE)],
    )
    def test_non_restored_phase_cannot_return_completed(self, incomplete, expected):
        optimizer, manifest, backend, defaults, config, measure = _fixture()
        baseline = backend.snapshot(manifest.items, fencing_token=7)
        actual = self._changed_snapshot(baseline, incomplete=incomplete)
        result = _run(
            optimizer,
            defaults,
            config,
            measure,
            phase_baseline_snapshot=baseline,
            current_snapshot=lambda: actual,
        )
        assert result.phase_restoration is not None
        assert result.phase_restoration.status is expected
        assert result.stop_reason is EngineStopReason.PHASE_RESTORATION_FAILED
        assert "failed closed" in result.stop_detail
