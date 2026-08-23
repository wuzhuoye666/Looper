from __future__ import annotations

import pytest
from looper_core.system_opt.component import ComponentOptimizer
from looper_core.system_opt.demo import (
    SyntheticMeasurementAdapter,
    build_demo_manifest,
    build_demo_policy,
    resolve_demo_domains,
)
from looper_core.system_opt.engine import (
    EngineLoopConfig,
    EngineStopReason,
    run_engine_loop,
)
from looper_core.system_opt.engine.loop import EngineRoundRecord
from looper_core.system_opt.executor.simulated import SimulatedBackend
from looper_core.system_opt.negative_cache import (
    NegativeCache,
    NegativeCacheEntry,
    NegativeCacheIdentity,
    NegativeVerdict,
    candidate_parameters_digest,
    formula_versions_digest,
)
from looper_core.system_opt.policy import OptimizationMode
from looper_core.system_opt.rollback import RestorationStatus
from looper_core.system_opt.tuning import SystemOptimizationEngine

ENV = "sha256:" + "5" * 64
FORMULAS = {"F-DEMO-LOOP": "v0"}
FIXED_PROTOCOL = "sha256:" + "6" * 64


def _optimizers(components: list[str], backend: SimulatedBackend):
    manifest = build_demo_manifest()
    result = []
    for component in components:
        policy = build_demo_policy(OptimizationMode.GENERAL)
        policy.authorized_components = [component]
        policy.search.max_candidates = 2
        policy.search.max_attempts = 4
        policy.search.no_improvement_limit = 3
        policy.search.target_improvement = None
        engine = SystemOptimizationEngine(policy, manifest, resolve_demo_domains(manifest), backend)
        result.append(ComponentOptimizer(engine))
    return result, manifest


def _config(max_rounds: int = 10) -> EngineLoopConfig:
    return EngineLoopConfig(
        environment_digest=ENV,
        formula_versions=FORMULAS,
        pressure_protocol_digests={"cpu": FIXED_PROTOCOL, "memory": FIXED_PROTOCOL},
        max_rounds=max_rounds,
        max_pool_size=64,
    )


def _baseline_parameters(manifest) -> dict[str, dict]:
    defaults = {item.parameter_id: item.default for item in manifest.items}
    return {"cpu": defaults, "memory": defaults}


class TestEngineLoop:
    def test_completes_all_components_with_verdicts_and_cache_writes(self):
        manifest = build_demo_manifest()
        initial = {item.id: item.default for item in manifest.items}
        backend = SimulatedBackend(initial, target_id="engine-loop-test")
        optimizers, manifest = _optimizers(["cpu", "memory"], backend)
        cache = NegativeCache()
        result = run_engine_loop(
            optimizers,
            baseline_parameters=_baseline_parameters(manifest),
            measures={
                "cpu": SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL),
                "memory": SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL),
            },
            negative_cache=cache,
            config=_config(),
            fencing_token=3,
        )
        assert result.stop_reason is EngineStopReason.COMPLETED
        assert [record.component for record in result.rounds] == ["cpu", "memory"]
        for record in result.rounds:
            assert record.verdicts, "every round must judge its candidates"
            assert all(v.candidate_id for v in record.verdicts)
        rejected = sum(
            1
            for record in result.rounds
            for verdict in record.verdicts
            if not verdict.accepted
        )
        assert len(cache) == rejected
        assert result.phase_restoration is None
        assert "skipped" in result.phase_verification_note

    def test_round_budget_stops_explicitly(self):
        manifest = build_demo_manifest()
        initial = {item.id: item.default for item in manifest.items}
        backend = SimulatedBackend(initial, target_id="round-budget-test")
        optimizers, manifest = _optimizers(["cpu", "memory"], backend)
        result = run_engine_loop(
            optimizers,
            baseline_parameters=_baseline_parameters(manifest),
            measures={
                "cpu": SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL),
                "memory": SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL),
            },
            negative_cache=NegativeCache(),
            config=_config(max_rounds=1),
            fencing_token=3,
        )
        assert result.stop_reason is EngineStopReason.ROUND_BUDGET
        assert len(result.rounds) == 1

    def test_fully_cached_pools_stop_before_any_round(self):
        from datetime import UTC, datetime

        fixed_at = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
        manifest = build_demo_manifest()
        initial = {item.id: item.default for item in manifest.items}
        backend = SimulatedBackend(initial, target_id="all-cached-test")
        optimizers, manifest = _optimizers(["cpu", "memory"], backend)
        cache = NegativeCache()
        
        fixed_at = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
        for optimizer in optimizers:
            protocol = FIXED_PROTOCOL
            for parameters in optimizer.candidate_pool():
                cache.add(
                    NegativeCacheEntry(
                        identity=NegativeCacheIdentity(
                            environment_digest=ENV,
                            candidate_parameters_digest=candidate_parameters_digest(parameters),
                            pressure_protocol_digest=protocol,
                            formula_versions_digest=formula_versions_digest(FORMULAS),
                        ),
                        metric_id="demo.metric",
                        verdict=NegativeVerdict.NO_IMPROVEMENT_LCB,
                        evidence_digests=["sha256:" + "7" * 64],
                        detail="previously disproven",
                        recorded_at=fixed_at,
                    )
                )
        result = run_engine_loop(
            optimizers,
            baseline_parameters=_baseline_parameters(manifest),
            measures={
                "cpu": SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL),
                "memory": SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL),
            },
            negative_cache=cache,
            config=_config(),
            fencing_token=3,
        )
        assert result.stop_reason is EngineStopReason.ALL_CACHED
        assert result.rounds == []

    def test_phase_ending_gate_verifies_restoration(self):
        manifest = build_demo_manifest()
        initial = {item.id: item.default for item in manifest.items}
        backend = SimulatedBackend(initial, target_id="phase-gate-test")
        optimizers, manifest = _optimizers(["cpu"], backend)
        baseline_snapshot = backend.snapshot(manifest.items, fencing_token=3)
        result = run_engine_loop(
            optimizers,
            baseline_parameters=_baseline_parameters(manifest),
            measures={"cpu": SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL)},
            negative_cache=NegativeCache(),
            config=EngineLoopConfig(
                environment_digest=ENV,
                formula_versions=FORMULAS,
                pressure_protocol_digests={"cpu": FIXED_PROTOCOL},
                max_rounds=5,
                max_pool_size=64,
            ),
            fencing_token=3,
            phase_baseline_snapshot=baseline_snapshot,
            current_snapshot=lambda: backend.snapshot(manifest.items, fencing_token=3),
        )
        assert result.phase_restoration is not None
        assert result.phase_restoration.status is RestorationStatus.RESTORED

    def test_duplicate_components_rejected(self):
        manifest = build_demo_manifest()
        initial = {item.id: item.default for item in manifest.items}
        backend = SimulatedBackend(initial, target_id="dup-test")
        optimizers, manifest = _optimizers(["cpu", "cpu"], backend)
        with pytest.raises(ValueError, match="duplicate components"):
            run_engine_loop(
                optimizers,
                baseline_parameters=_baseline_parameters(manifest),
                measures={"cpu": object()},
                negative_cache=NegativeCache(),
                config=_config(),
                fencing_token=3,
            )

    def test_round_record_round_trips_through_digest(self):
        record = EngineRoundRecord(
            round_index=1,
            component="cpu",
            report_digest="sha256:" + "8" * 64,
            selected_parameters={"k": "v"},
            skipped=[],
            verdicts=[],
            cache_entry_digests=[],
        )
        assert EngineRoundRecord.model_validate_json(record.model_dump_json()) == record


class TestOptimizations:
    def _cache_entry_for(self, parameters, metric="demo.metric"):
        from datetime import UTC, datetime

        from looper_core.system_opt.negative_cache import NegativeCacheIdentity

        return NegativeCacheEntry(
            identity=NegativeCacheIdentity(
                environment_digest=ENV,
                candidate_parameters_digest=candidate_parameters_digest(parameters),
                pressure_protocol_digest=FIXED_PROTOCOL,
                formula_versions_digest=formula_versions_digest(FORMULAS),
            ),
            metric_id=metric,
            verdict=NegativeVerdict.NO_IMPROVEMENT_LCB,
            evidence_digests=["sha256:" + "9" * 64],
            detail="cached in an earlier engine run",
            recorded_at=datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC),
        )

    def test_safety_needs_attention_stops_engine_immediately(self):
        from looper_core.system_opt.executor.simulated import SimulatedFailurePlan

        manifest = build_demo_manifest()
        initial = {item.id: item.default for item in manifest.items}
        backend = SimulatedBackend(
            initial,
            target_id="safety-stop-test",
            failure_plan=SimulatedFailurePlan(
                rollback_failures={item.id for item in manifest.items}
            ),
        )
        optimizers, manifest = _optimizers(["cpu", "memory"], backend)
        result = run_engine_loop(
            optimizers,
            baseline_parameters=_baseline_parameters(manifest),
            measures={
                "cpu": SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL),
                "memory": SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL),
            },
            negative_cache=NegativeCache(),
            config=_config(),
            fencing_token=3,
        )
        assert result.stop_reason is EngineStopReason.SAFETY_STOP
        assert len(result.rounds) == 1
        assert "needs-attention" in result.stop_detail

    def test_cached_candidate_is_excluded_from_component_search(self):
        manifest = build_demo_manifest()
        initial = {item.id: item.default for item in manifest.items}
        backend = SimulatedBackend(initial, target_id="exclusion-test")
        optimizers, manifest = _optimizers(["cpu"], backend)
        cached_params = optimizers[0].candidate_pool()[0]
        # The only non-baseline candidate is cached. The baseline must not be
        # disguised as a new candidate measurement.
        cache = NegativeCache([self._cache_entry_for(cached_params)])
        result = run_engine_loop(
            optimizers,
            baseline_parameters=_baseline_parameters(manifest),
            measures={"cpu": SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL)},
            negative_cache=cache,
            config=_config(),
            fencing_token=3,
        )
        assert result.rounds == []
        assert result.stop_reason is EngineStopReason.ALL_CACHED

    def test_pool_cap_exceeded_fails_closed(self):
        manifest = build_demo_manifest()
        initial = {item.id: item.default for item in manifest.items}
        backend = SimulatedBackend(initial, target_id="cap-test")
        optimizers, manifest = _optimizers(["cpu"], backend)
        with pytest.raises(ValueError, match="max_pool_size"):
            run_engine_loop(
                optimizers,
                baseline_parameters=_baseline_parameters(manifest),
                measures={
                    "cpu": SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL),
                },
                negative_cache=NegativeCache(),
                config=EngineLoopConfig(
                    environment_digest=ENV,
                    formula_versions=FORMULAS,
                    pressure_protocol_digests={"cpu": FIXED_PROTOCOL},
                    max_rounds=5,
                    max_pool_size=1,
                ),
                fencing_token=3,
            )

    def test_fully_excluded_component_records_note_not_error(self):
        manifest = build_demo_manifest()
        initial = {item.id: item.default for item in manifest.items}
        backend = SimulatedBackend(initial, target_id="excluded-test")
        optimizers, manifest = _optimizers(["cpu"], backend)
        cache = NegativeCache(
            [self._cache_entry_for(params) for params in optimizers[0].candidate_pool()]
        )
        # Full-pool cache makes the scheduler skip the component before running;
        # assert that path instead of an executed empty search.
        result = run_engine_loop(
            optimizers,
            baseline_parameters=_baseline_parameters(manifest),
            measures={"cpu": SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL)},
            negative_cache=cache,
            config=_config(),
            fencing_token=3,
        )
        assert result.stop_reason is EngineStopReason.ALL_CACHED
        assert result.rounds == []


class TestPreScreenWiring:
    def test_tolerance_enabled_loop_runs_and_records_incumbent(self):
        manifest = build_demo_manifest()
        initial = {item.id: item.default for item in manifest.items}
        backend = SimulatedBackend(initial, target_id="prescreen-test")
        optimizers, manifest = _optimizers(["cpu"], backend)
        result = run_engine_loop(
            optimizers,
            baseline_parameters=_baseline_parameters(manifest),
            measures={"cpu": SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL)},
            negative_cache=NegativeCache(),
            config=EngineLoopConfig(
                environment_digest=ENV,
                formula_versions=FORMULAS,
                pressure_protocol_digests={"cpu": FIXED_PROTOCOL},
                max_rounds=3,
                max_pool_size=64,
                pre_screen_tolerance=0.0,
            ),
            fencing_token=3,
        )
        assert result.stop_reason is EngineStopReason.COMPLETED
        record = result.rounds[0]
        assert record.incumbent_utility_after is not None

    def test_incumbent_pre_screen_is_isolated_per_component(self):
        # C3 regression (SO-D018): a cpu incumbent must never screen out a
        # memory candidate. Each component's first observation is always
        # FIRST_OBSERVATION inside its own tracker, whatever the other
        # component's incumbent utility is.
        manifest = build_demo_manifest()
        initial = {item.id: item.default for item in manifest.items}
        backend = SimulatedBackend(initial, target_id="prescreen-isolated-test")
        optimizers, manifest = _optimizers(["cpu", "memory"], backend)
        result = run_engine_loop(
            optimizers,
            baseline_parameters=_baseline_parameters(manifest),
            measures={
                "cpu": SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL),
                "memory": SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL),
            },
            negative_cache=NegativeCache(),
            config=EngineLoopConfig(
                environment_digest=ENV,
                formula_versions=FORMULAS,
                pressure_protocol_digests={"cpu": FIXED_PROTOCOL, "memory": FIXED_PROTOCOL},
                max_rounds=3,
                max_pool_size=64,
                pre_screen_tolerance=0.0,
            ),
            fencing_token=3,
        )
        assert result.stop_reason is EngineStopReason.COMPLETED
        records = {record.component: record for record in result.rounds}
        assert set(records) == {"cpu", "memory"}
        for component, record in records.items():
            first_candidate_id = record.verdicts[0].candidate_id
            assert first_candidate_id not in record.early_screened_candidate_ids, (
                f"component {component} screened its first observation against "
                "another component's incumbent; trackers must be isolated (SO-D018)"
            )
            assert record.incumbent_utility_after is not None, (
                f"component {component} must record its own incumbent"
            )


DIGEST = "sha256:" + "4" * 64


class TestPromotionObservations:
    def test_promotion_observation_only_for_accepted(self):
        from looper_core.system_opt.engine.judge import CandidateVerdict
        from looper_core.system_opt.engine.loop import promotion_observation
        
        accepted = CandidateVerdict(
            candidate_id="c1", comparable=True, feasible=True, accepted=True,
            reasons=["S7: ok"], primary_metric="m", minimum_effect=0.0,
        )
        rejected = accepted.model_copy(update={"accepted": False})
        class _C:
            candidate_id = "c1"

        obs = promotion_observation(
            round_index=2, environment_digest=ENV,
            candidate=_C(), verdict=accepted, evidence_digest=DIGEST,
        )
        assert obs is not None and obs.passed and obs.time_block_id == "engine-round-2"
        assert promotion_observation(
            round_index=2, environment_digest=ENV,
            candidate=_C(), verdict=rejected, evidence_digest=DIGEST,
        ) is None

    def test_engine_emits_observations_when_contract_present(self):
        from looper_core.system_opt.result_vector import PromotionContract

        manifest = build_demo_manifest()
        initial = {item.id: item.default for item in manifest.items}
        backend = SimulatedBackend(initial, target_id="s9-test")
        optimizers, manifest = _optimizers(["cpu"], backend)
        result = run_engine_loop(
            optimizers,
            baseline_parameters=_baseline_parameters(manifest),
            measures={"cpu": SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL)},
            negative_cache=NegativeCache(),
            config=EngineLoopConfig(
                environment_digest=ENV,
                formula_versions=FORMULAS,
                pressure_protocol_digests={"cpu": FIXED_PROTOCOL},
                max_rounds=3,
                max_pool_size=64,
                promotion_contract=PromotionContract(
                    min_observations=1, min_distinct_time_blocks=1, min_environments=1
                ),
            ),
            fencing_token=3,
        )
        emitted = [
            obs for record in result.rounds for obs in record.promotion_observations
        ]
        accepted_count = sum(
            1 for record in result.rounds for verdict in record.verdicts if verdict.accepted
        )
        assert len(emitted) == accepted_count
