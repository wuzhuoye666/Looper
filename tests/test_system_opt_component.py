from __future__ import annotations

import pytest

from looper_core.system_opt.component import (
    CandidateSuggestion,
    ComponentOptimizer,
    ComponentReport,
    NO_FINAL_VERDICT_NOTE,
    NullFormulaMapping,
)
from looper_core.system_opt.demo import (
    SyntheticMeasurementAdapter,
    build_demo_manifest,
    build_demo_policy,
    resolve_demo_domains,
)
from looper_core.system_opt.engine import evaluate_candidate
from looper_core.system_opt.executor.simulated import SimulatedBackend
from looper_core.system_opt.policy import OptimizationMode
from looper_core.system_opt.tuning import SystemOptimizationEngine


def _engine(component: str = "cpu"):
    manifest = build_demo_manifest()
    initial = {item.id: item.default for item in manifest.items}
    backend = SimulatedBackend(initial, target_id="component-wrapper-test")
    policy = build_demo_policy(OptimizationMode.GENERAL)
    policy.authorized_components = [component]
    policy.search.max_candidates = 2
    policy.search.max_attempts = 4
    policy.search.no_improvement_limit = 3
    policy.search.target_improvement = None
    engine = SystemOptimizationEngine(policy, manifest, resolve_demo_domains(manifest), backend)
    return engine, backend


def _run(optimizer: ComponentOptimizer, backend: SimulatedBackend) -> ComponentReport:
    manifest = optimizer.engine.manifest
    return optimizer.run(
        baseline_parameters={item.parameter_id: item.default for item in manifest.items},
        measure=SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL),
        fencing_token=9,
    )


class TestComponentWrapperContract:
    def test_wrapper_requires_exactly_one_component(self):
        manifest = build_demo_manifest()
        backend = SimulatedBackend(
            {item.id: item.default for item in manifest.items}, target_id="multi"
        )
        engine = SystemOptimizationEngine(
            build_demo_policy(OptimizationMode.WORKLOAD), manifest,
            resolve_demo_domains(manifest), backend,
        )
        assert len(engine.policy.authorized_components) > 1
        with pytest.raises(ValueError, match="exactly one component"):
            ComponentOptimizer(engine)

    def test_report_carries_run_without_final_verdict_semantics(self):
        engine, backend = _engine()
        report = _run(ComponentOptimizer(engine), backend)
        assert report.component == "cpu"
        assert report.run_digest is not None
        assert report.stop_reason is not None
        assert report.semantic_note == NO_FINAL_VERDICT_NOTE
        assert report.promotion_suggestions == [
            candidate.candidate_id
            for candidate in report.candidates
            if candidate.accepted
        ]
        assert ComponentReport.model_validate_json(report.model_dump_json()).digest == report.digest


class TestFormulaMappingHook:
    def test_null_mapping_returns_empty_and_report_stays_empty(self):
        engine, backend = _engine()
        optimizer = ComponentOptimizer(engine)
        assert optimizer.suggest_candidates() == []
        report = _run(optimizer, backend)
        assert report.formula_suggestions == []

    def test_custom_mapping_suggestions_are_recorded_in_report(self):
        engine, backend = _engine()
        parameter_id = next(
            item.parameter_id
            for item in engine.manifest.items
            if item.primary_component.value == "cpu"
        )
        suggestion = CandidateSuggestion(
            parameters={parameter_id: engine.manifest.item_for_parameter(parameter_id).default},
            rationale="demo mapping keeps the declared default",
            formula_id="F-DEMO-MAPPING/v0",
        )

        class StaticMapping:
            def suggest(self, snapshot, baseline):
                return [suggestion]

        optimizer = ComponentOptimizer(engine, StaticMapping())
        assert optimizer.suggest_candidates() == [suggestion]
        report = _run(optimizer, backend)
        assert report.formula_suggestions == [suggestion]

    def test_null_mapping_is_the_default(self):
        engine, _ = _engine()
        optimizer = ComponentOptimizer(engine)
        assert isinstance(optimizer.formula_mapping, NullFormulaMapping)


class TestFinalVerdictBelongsToEngine:
    def test_every_report_candidate_gets_an_engine_verdict(self):
        engine, backend = _engine()
        report = _run(ComponentOptimizer(engine), backend)
        assert report.candidates, "demo loop must produce candidates"
        primary = engine.policy.primary_metric.id
        for candidate in report.candidates:
            verdict = evaluate_candidate(
                candidate, primary_metric=primary, minimum_effect=0.0
            )
            assert verdict.candidate_id == candidate.candidate_id
            assert verdict.reasons
        # The report itself never carries an engine-level verdict field.
        assert not hasattr(report, "verdict")
        assert not hasattr(report, "accepted")
