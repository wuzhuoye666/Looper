"""System Optimizer 包门面（L1 配置底座公开 API）。

对外 re-export 配置清单、配置档与安全执行的核心类型，供上层（引擎、CLI）统一导入；
本文件不含逻辑，仅为兼容性门面。
"""

from looper_core.system_opt.config_manifest import (
    ActivationMode,
    CommandTemplate,
    ConfigCategory,
    ConfigItem,
    ConfigManifest,
    ConfigValueType,
    RiskLevel,
    SystemTuningBinding,
    ValueDomain,
)
from looper_core.system_opt.hypothesis import (
    CapacityDecisionStatus,
    CapacityFrontierDecision,
    HypothesisEvidence,
    HypothesisState,
    OptimizationHypothesis,
    evaluate_capacity_frontiers,
    hypothesis_context_digest,
    rank_authorized_hypotheses,
)
from looper_core.system_opt.profiles import (
    ExpandedProfile,
    ProfileExpansionError,
    ProfileRepository,
    TuningProfile,
)
from looper_core.system_opt.safety import (
    SafetyController,
    SafetyPolicy,
    SafetyResult,
    SafetyState,
)

__all__ = [
    "ActivationMode",
    "CommandTemplate",
    "ConfigCategory",
    "ConfigItem",
    "ConfigManifest",
    "ConfigValueType",
    "CapacityDecisionStatus",
    "CapacityFrontierDecision",
    "ExpandedProfile",
    "HypothesisEvidence",
    "HypothesisState",
    "OptimizationHypothesis",
    "ProfileExpansionError",
    "ProfileRepository",
    "RiskLevel",
    "SafetyController",
    "SafetyPolicy",
    "SafetyResult",
    "SafetyState",
    "SystemTuningBinding",
    "TuningProfile",
    "ValueDomain",
    "evaluate_capacity_frontiers",
    "hypothesis_context_digest",
    "rank_authorized_hypotheses",
]
