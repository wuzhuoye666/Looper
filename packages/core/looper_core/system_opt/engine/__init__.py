"""L8 总优化器引擎：调度 · 判断 · 打分三器官。

架构层：总体架构 v2 的 L8（见 docs/system-optimizer/architecture/overall.md）。
引擎不做测量、不直接写配置、不私藏门禁；计算规则唯一来源是
docs/system-optimizer/contracts/formula-provenance.md（S0–S10）。

- scorer  ：S4 组件优先级编排与组件排序
- judge   ：S0 可比 → S2 硬门禁 → S7 接受 的固定顺序裁决
- scheduler：组件顺序 + L7 负缓存 → 选出下一组 (组件, 候选)
"""

from looper_core.system_opt.engine.judge import CandidateVerdict, evaluate_candidate
from looper_core.system_opt.engine.scheduler import (
    SchedulerDecision,
    SchedulerSelection,
    SkippedCandidate,
    select_next_candidate,
)
from looper_core.system_opt.engine.scorer import (
    ComponentScore,
    rank_components,
    score_components,
)

__all__ = [
    "CandidateVerdict",
    "ComponentScore",
    "SchedulerDecision",
    "SchedulerSelection",
    "SkippedCandidate",
    "evaluate_candidate",
    "rank_components",
    "score_components",
    "select_next_candidate",
]
