from __future__ import annotations

import json
from pathlib import Path

from looper_api.verified_demo import DEFAULT_COMPRESSION_STATE, run_verified_compression_loop
from looper_core.action_loop import ActionDecision, VerificationPolicy


def test_verified_compression_demo_runs_real_benchmark_and_restores_baseline(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    result = run_verified_compression_loop(
        tmp_path / "verified-demo",
        candidate={"compression_level": 9, "chunk_size": 4096},
        policy=VerificationPolicy(
            repeats=2,
            minimum_improvement_ratio=5.0,
            maximum_secondary_regression_ratio=1.0,
            confidence_level=0.95,
            bootstrap_resamples=200,
            random_seed=11,
        ),
        size_kib=128,
        samples=3,
        timeout_seconds=60,
        repository_root=repository_root,
    )

    assert result["decision"] in {
        ActionDecision.ROLLED_BACK,
        ActionDecision.INCONCLUSIVE,
    }
    assert result["rollbackVerified"] is True
    assert result["finalState"]["value"] == DEFAULT_COMPRESSION_STATE
    state = json.loads(Path(result["stateFile"]).read_text(encoding="utf-8"))
    assert state == DEFAULT_COMPRESSION_STATE
    decision_file = Path(result["decisionFile"])
    assert decision_file.is_file()
    persisted = json.loads(decision_file.read_text(encoding="utf-8"))
    assert persisted["rollbackVerified"] is True
    assert len(persisted["measurements"]["baseline"]) == 2
    assert len(persisted["measurements"]["candidate"]) == 2
    assert all(
        Path(item["evidence"]["outputDirectory"]).joinpath("result.json").is_file()
        for item in persisted["measurements"]["baseline"] + persisted["measurements"]["candidate"]
    )
