# S4-01：组件内优先级 schema / 公式版本设计冻结（R2）

> 状态：design-only（R2，按主 agent 方向修正）。不实现代码。
> 基线：`origin/system-optimizer-impl@4ed860c`。
> R2 变更：①写集合矛盾消除（S4-01 不切生产入口，V2 迁移 deferred）；②S3-01 接口改为
> `OnlineHypothesisSource.__call__(symptom)`；③排序键不改序，沿用现 v1 四维词典序。
> 日期：2026-08-24。

## 1. 结论（R2 定调）

1. **S4-01 本轮冻结现四维 v1，不做任何生产代码改动**。`DiagnosticPriority`
   (`pressure/adverse_change/persistence/confidence`)、公式
   `F-PROJECT-S4-PIECEWISE-LINEAR/v1alpha1`、`diagnostic_priorities()`、
   `score_components()`、`ComponentScore` **全部原样保留**。
2. **P/D/A/Q/T V2 迁移 deferred**：`confidence` 错名（样本充足率）延后到语义正式定义后，
   与 V2 schema + M9 迁移合并为未来单一版本事件。本轮不新增 `DiagnosticPriorityV2`、
   不分派、不切入口。
3. **S3-01 不依赖 V2**：直接基于现有四维向量做在线 rank，接口见 §7，可立即进入开发。
4. **E_m 继续禁用**；`scale`/阈值/metric→组件映射继续为任务显式输入，无默认值。

## 2. 现有实现盘点（不变，供 S3-01 复用）

- `DiagnosticPriority`（scoring.py:148-167）：`metric_id/component/pressure/adverse_change/
  persistence/confidence/pareto_rank/formula_id/current_batch_digest/reference_batch_digest`
  （10 字段，无 `schema_version`）。`confidence` = `min(1.0, n/minimum_samples)`（样本充足率，
  scoring.py:154-156 注释确认错名，本轮不改）。
- 公式 `F-PROJECT-S4-PIECEWISE-LINEAR/v1alpha1`：`pressure_value()`(scoring.py:286) +
  `adverse_change()`(scoring.py:344)，`scale_m` 缺失 fail-closed。
- 入口 `diagnostic_priorities()`(scoring.py:520)，排序键：
  `(pareto_rank, component, -pressure, -adverse_change, -persistence, -confidence, metric_id)`。
- L8 `score_components()`(engine/scorer.py:36) → `ComponentScore.priorities_digest`。
- L5 `tuning.py:257` 读 `priority.component` 路由。
- 方向-方法相容表 `MetricContract.validate_semantics`(policy.py:141-165)。
- 分派先例：`load_optimization_run`（test_system_opt_optimization_run_versions.py）。

## 3. R2 修正一：写集合矛盾 → S4-01 零代码改动

R1 曾写「`diagnostic_priorities()` 只产 V2」却禁止改 scorer/tuning，与
`score_components()` 显式读 `pressure/adverse_change` 冲突。R2 选择主 agent 给出的
**02A 方向**：S4-01 **不新增 V2/load、不切入口**。S4-01 唯一交付 = 本文档。

- 生产链（scoring → engine/scorer → tuning）不受任何影响。
- V2 迁移独立成「S4-V2（deferred）」任务：语义正式定义后，一次性完成
  `DiagnosticPriorityV2` + `load_diagnostic_priority` 分派 + scorer/tuning/ComponentScore
  版本分派 + digest 锚 + M9 迁移；写集合届时再定（预计 scoring + engine/scorer + tuning +
  ComponentScore + 专属测试）。

## 4. R2 修正二：S3-01 接口带 symptom

`ComponentHypothesis` 必带 `symptom_id`，R1 的 `ranked_proposals(priorities, proposal_source)`
  无 `SymptomRecord`，无法构造合法输出。R2 改为与现 `FileHypothesisProposals.__call__`
  （dynamic_adapters.py:357）同形：

```python
class OnlineHypothesisSource:
    def __call__(self, symptom: SymptomRecord) -> list[ComponentHypothesis]: ...
```

- 内部读取 `HypothesisProposalV2`（dynamic_adapters.py:225，含 `change/risk/risk_kind/
  risk_rationale`）的 `by_id()`。
- **只在线替换 `rank`**：用 O1 evidence → `diagnostic_priorities()`（现四维）得到
  `(component → pareto_rank, P, D, persistence, confidence)`，据此重排 proposal 的 rank。
- `ComponentHypothesis` 仍带 `symptom_id=symptom.symptom_id`、`component`、`supporting_digests`；
  `change`/`risk` **不复制进 hypothesis**，仍由 intervention adapter 按 proposal id 获取
  （与现 D5-I2-B 的 proposal→plan 路径一致）。
- 数据不足 / 身份漂移 / pressure-protocol 不一致 → `diagnostic_priorities` 现语义 fail-closed，
  本类不吞异常。
- 输出确定性：同 symptom 同 O1 证据 → 同排序（排序键见 §5）。

## 5. R2 修正三：排序键不改序

S3-01 在线 rank 复用现 `diagnostic_priorities` 的**完全一致**词典序：

```
(pareto_rank, component, -pressure, -adverse_change, -persistence, -confidence, metric_id)
```

- 字段名与顺序都不改，因此 R1 的「Q 在 T 前」候选键作废；**等价声明成立**（逐字一致）。
- `component` 内 Pareto 层（`_assign_component_pareto_ranks`）与 `_priority_dominates`
  四维支配不变；跨组件仍按 `authorized_components` 顺序，不从 P/D 伪造总严重度。

## 6. scale / 阈值 / metric 映射（不变）

`scale_m` 每 metric 合同显式（`MetricContract.scale`，`gt=0`）；解释阈值、metric→组件映射、
四象限高/低全部任务显式输入，S4-02 逐项校准；S4-01/S3-01 均不填数值、不引入隐式分母。

## 7. E_m（不变）

`E_m` 未定义未实现（formula-provenance.md:692）；无同环境校准分布，**保持禁用**；提案须先
`PROJECT-DRAFT` 进登记表。

## 8. 负向测试矩阵（S3-01 视角，不涉及 S4 代码改动）

| # | 用例 | 预期 |
|---|---|---|
| N1 | 同 symptom + 同 O1 证据两次调用 | 输出顺序逐字节一致（确定性） |
| N2 | 高 P/D 组件 proposal | 该组件 rank 前移 |
| N3 | O1 证据缺 metric / 样本 < minimum_samples | `InsufficientEvidence`，不产出 hypothesis |
| N4 | pressure-protocol 缺失/不一致 | `InsufficientEvidence` |
| N5 | 跨组件 | 不互相支配，按 authorized 顺序 |
| N6 | proposal id 无对应 O1 component 证据 | 该 proposal 保持文件 rank 或标记（待 §9 决策） |
| N7 | 声明式 v1/v2 fixture 回放 | 仍可加载，不冒充在线推导 |
| N8 | 现四维 `DiagnosticPriority` JSON 加载 | 字段集合与 digest 不变（V2 deferred） |

## 9. 历史工件影响（V2 deferred 后）

- 现四维 `DiagnosticPriority`、`ComponentScore.priorities_digest`、`tuning.py` run 记录、
  `MetricContract`/`MeasurementBatch`：**全部不变**，无迁移、无 digest 漂移。
- `HypothesisProposalV2`（v1alpha2）已存在，S3-01 只读消费，不改其 schema。

## 10. 后续拆包与最小写集合

- **S4-01**：docs-only，无代码写集合。
- **S3-01**：新增 `OnlineHypothesisSource` 模块 + 专属测试；**最小写集合** = 新模块 +
  新测试文件；只读 `HypothesisProposalV2`、`diagnostic_priorities`、`ComponentHypothesis`。
  不改 dynamic loop / CLI / negative cache / receipt / 公式实现。
- **S4-V2（deferred）**：语义定版后一次性迁移，写集合届时另报，不阻塞 S3-01。
- **S4-02 / M3-INT**：依赖顺序不变。

## 11. 待用户决策清单

1. **S3-01 对「无 O1 证据的 proposal」处置**：保留文件 rank、置末、还是报错（N6 待定）。
2. **在线 rank 是否写回 proposal 文件/产出独立 ranked 记录**：只在线替换、或另产
   `ranked_proposals_digest` 可回放工件。
3. **S4-V2（deferred）触发条件**：P/D/A/Q/T 语义由谁、何时正式定义；`confidence` 新名。
4. **公式版本**：V2 迁移时 `F-PROJECT-S4-PIECEWISE-LINEAR` 是否升 v1alpha2。
5. **E_m 提案起点**：确认无同环境校准分布则持续禁用。
6. **scale/阈值/metric 映射**：确认全部任务显式输入，S4-02 校准，本批不填数值。

## 12. 源码引用核实（4ed860c 已 grep/read 确认）

`scoring.py`(DiagnosticPriority:148, confidence 注释:154, diagnostic_priorities:520,
pressure_value:286, adverse_change:344, _priority_dominates:577)、
`policy.py`(MetricContract:57, scale:67, 相容表:141)、
`engine/scorer.py`(ComponentScore:22, score_components:36, priorities_digest:58)、
`tuning.py`(DiagnosticPriority:39, 消费:257)、
`dynamic_adapters.py`(HypothesisProposalV2:225, HypothesisProposalsFileV2:248,
FileHypothesisProposals.__call__:357, FileHypothesisProposalsV2.__call__:376)、
`hypothesis.py`(SymptomRecord:47, ComponentHypothesis:84)、
`formula-provenance.md`(S4 向量:123, F-PROJECT-002:533, E_m:692)、
`unfinished-task-queue-2026-08-24.md`(S4-01:102, S4-02:103, S3-01:104)、
`test_system_opt_policy_scoring.py`(436/447)、`test_system_opt_optimization_run_versions.py`(分派范式)。
