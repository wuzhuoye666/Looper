# phase-gate 合同修订设计 R3（两阶段干预接口 + 异常合同）

> 状态：D5-I1 实施中——模型与纯函数已落地于 `intervention.py`（
> `InterventionPlan` / `InterventionOutcome` / `InterventionExecutionReceipt` /
> `RiskSource` + `resolve_plan_risk` / `evaluate_intervention_gate` /
> `verify_outcome_binding`）；动态循环接线属 D5-I2。
> 关联：`phase_gate.py`、`intervention.py`、`dynamic_loop.py`、`hypothesis.py`、
> `dynamic_adapters.py`、`safety.py`、`config_manifest.py`（ConfigItem.risk / RiskLevel）。
> 日期：2026-08-24（R3 修订；D5-I1 实现对齐）。

## 1. 背景问题（主 agent 反馈，均确认）

1. 「禁止默认风险」与 `risky: bool = False` 自相矛盾——缺失字段被静默视为非风险。
2. `len(head.change)` 引用不存在的 `ComponentHypothesis.change`。
3. `intervention() is None` 不代表「未写入」——L1 可能已 apply 后失败/rollback。
4. （R3 新增）`plan_digest` 若是普通字段，可被伪造/自引用，不能作为回绑锚点。
5. （R3 新增）`execute_intervention` 无异常合同：意外异常时 `apply_started` 会丢失，
   预算计数无法据实。

## 2. 两阶段接口

```
prepare_intervention(hypothesis) -> InterventionPlan      # 纯规划，不写配置
    → 执行前门禁检查（single_change / risk_quota）        # dynamic_loop 里
    → execute_intervention(plan) -> InterventionOutcome   # 真正施加/复测/回退
```

## 3. InterventionPlan（提案）

```python
class InterventionPlan(StrictModel):
    schema_version: Literal[INTERVENTION_PLAN_SCHEMA]
    hypothesis: ComponentHypothesis            # 身份绑定
    change: dict[str, Any]                     # 具体变更（显式，非 head.change）
    risk: RiskLevel                            # 显式必填，无默认
    risk_source: RiskSource                    # 风险来源与一致性证据

    @property
    def change_count(self) -> int:             # = len(change)，计算属性
        return len(self.change)                # single-change 执行前检查用

    @property
    def digest(self) -> str:                   # canonical digest，非可伪造字段
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))
```

- **不使用 `plan_digest` 字段**：`digest` 是 `@property` 计算值，攻击者无法自报一个与内容不一致的 plan_digest。
- **不使用 `change_count` 字段**：`change_count` 是 `@property`（精确 `len(self.change)`），调用方无法伪造；`change` 为空在模型校验层拒绝。
- `InterventionOutcome.plan_digest` 回绑 `plan.digest`（即 `InterventionPlan.digest` 的返回值），不信任任何自报字段；绑定由 `verify_outcome_binding(outcome, plan)` 显式校验，非注释声称。
- `risk` **必填**（RiskLevel 枚举，无默认）；缺失即模型校验失败，不静默降级为 low。

## 4. 风险来源与一致性（不信任 proposal 自报）

1. **manifest 基准（下界）**：`ConfigItem.risk`（low/medium/high）；plan 涉及的所有项取**最高者**为 manifest 基准风险。
2. **任务风险只能抬高、不能降低**：任务可显式声明 higher，但**不得 lower**；最终风险取二者较高值。声明低于 manifest 最高风险时 fail-closed（无「理由豁免」路径）。
3. **不得仅凭 proposal 自报 `risky=False` 绕过 manifest 风险**；缺失任务风险、缺失 manifest 绑定、绑定不一致一律 fail-closed。

`RiskSource` 字段（版本化 `schema_version` 见 §10）：

- `kind`：`manifest-derived` / `task-override`，语义由 `resolve_plan_risk` 强制执行：
  - `plan.risk == manifest 最高风险` → 只允许 `manifest-derived`；
  - `plan.risk > manifest 最高风险` → 必须 `task-override` 且 rationale 非空；
  - `plan.risk < manifest 最高风险` → fail-closed；
  - kind / risk / rationale 三者不一致一律 `InterventionContractError`。
- `manifest_digest`：绑定声明来源的 manifest digest。
- `items`：逐项 `RiskSourceItem{item_id, risk}`，**集合语义**——必须按 `item_id` 严格升序、且不重复；反序/重复直接拒绝（不静默排序），防止调用方靠重排改变 plan digest。

## 5. InterventionOutcome（提案）

```python
class InterventionOutcome(StrictModel):
    schema_version: Literal[INTERVENTION_OUTCOME_SCHEMA]
    plan_digest: str                            # 回绑 InterventionPlan.digest（计算值）
    write_attempted: bool                       # 是否尝试过写配置
    apply_started: bool                         # L1 apply 是否已开始（预算计数的真信号）
    rollback_attempted: bool
    rollback_verified: bool
    experiment: InterventionExperiment | None   # 业务复测是否产出实验（S6/S7）
    safety_state: SafetyState
    evidence_digest: str                        # safety result / measurement batch digest
```

- `InterventionOutcome` 内部同样强制进度链：`apply_started => write_attempted`、`rollback_attempted => apply_started`、`rollback_verified => rollback_attempted`（模型校验层，非注释）。
- 回绑由显式函数 `verify_outcome_binding(outcome, plan)` 校验：`outcome.plan_digest != plan.digest` 即 `InterventionContractError`。

## 6. execute_intervention 异常合同

- **正常操作性失败**（apply 失败、verify 失败、业务复测拒绝、回退失败等）：在开始执行后**必须返回 `InterventionOutcome`**，不抛异常；`write_attempted`/`apply_started`/`rollback_*` 如实反映进展。
- **意外异常**（未预期的 bug/崩溃）：必须有**执行 receipt/journal** 记录 `apply_started` 至少落盘。receipt 是**新建独立、版本化的 `InterventionExecutionReceipt`**（`looper.intervention-execution-receipt/v1alpha1`），**不复用** `SafetyController` 当前临时的 `control/intervention-failure-*.json` 通道；其 digest 可重算并绑定 `plan.digest`，状态只能前进不能倒退。receipt 本身写不进时也要 best-effort 落盘并保留原始异常。
- **D5-I1 边界**：本阶段只落地 `InterventionExecutionReceipt` 的**模型与纯函数**（进度链约束 + `advance` 单调推进），**不接 backend、不伪称崩溃后已持久化**；真正落盘与异常恢复接线属 D5-I2。
- **dynamic_loop 异常路径**：捕获 `execute_intervention` 的意外异常后，**据 receipt 计 intervention/risk**（`apply_started=True` 即计），随后 fail-closed（停止本相位并记录原始异常 + receipt digest）。不因异常丢失预算计数。

## 7. 预算计数口径

| 情况 | 计 intervention | 计 risk |
|---|---|---|
| 仅 prepare（proposed） | 否 | 否 |
| 执行前门禁拒绝（single-change / risk_quota） | 否 | 否 |
| `apply_started=True`（含 rollback / 无 Experiment / 意外异常） | **是** | 若 plan.risk 非 low 则**是** |
| 纯 preflight 拒绝（apply 之前） | **否** | **否** |

## 8. risk_quota 语义（执行前检查）

`risk_quota` = 「最多允许**执行**的 risky 干预数」。执行前检查（`execute_intervention` 之前）：

```text
resolved = resolve_plan_risk(plan, manifest)          # 强制：自报 low 不能绕过 high manifest
if resolved.final_risk != low and risky_interventions >= risk_quota:
    生成停止（不执行本次 plan）
```

判定用 `>=`，风险以 `resolved.final_risk`（而非 `plan.risk`）为准；删除 `evaluate_phase_gate` 现存 `risky_interventions > risk_quota` 停类分支（由执行前检查接管，D5-I2 接线后移除）。门禁输入严格校验：`risky_interventions` 非负整数（bool 不算）、`evidence_digest` 严格 sha256。

## 9. stop decision 的生成（不是抽象 stop()）

执行前门禁被拒时生成现有 `GateDecision`：

- `stop_class = GateStopClass.BUDGET_EXHAUSTED`；
- `triggered_field`：single-change 拒填 `"single_change_per_window"`，risk_quota 拒填 `"budget.risk_quota"`；
- `reason`：写明「下一次 risky 干预前 current >= quota（current=…, quota=…）」；
- `contract_digest` = `gate_contract.digest`；`evidence_digest` = 当前 `gate_state.evidence_digest`。

`run_dynamic_phase` 把该 `GateDecision` 写入 `DynamicPhaseRun.stop_gate_decision`。

## 10. schema 版本与 legacy 加载策略

- 新 schema 明确版本化，只增不删：
  - `InterventionPlan` → `looper.intervention-plan/v1alpha1`
  - `InterventionOutcome` → `looper.intervention-outcome/v1alpha1`
  - `RiskSource` → `looper.intervention-risk-source/v1alpha1`（`schema_version` 为必填 Literal）
  - `InterventionExecutionReceipt` → `looper.intervention-execution-receipt/v1alpha1`
- `InterventionOutcome.experiment` 保留旧 `InterventionExperiment` 子模型，`accepted`/`business_lcb` 语义不变。
- **新增必填字段不得静默改变历史 digest**：历史产物不加必填字段、不改 digest 口径；新字段全在新 schema。若未来给 `ComponentHypothesis` 加 risk，需评估对 `HypothesisLedger.digest` 的影响（沿用 `test_system_opt_optimization_run_versions.py` 的分派加载策略）。

## 11. 状态迁移表

| 事件 | interventions | risky_interventions |
|---|---|---|
| prepare 成功 | 不变 | 不变 |
| 执行前门禁拒绝 | 不变 | 不变 |
| apply_started=True（任何终态，含异常） | +1 | plan.risk!=low 时 +1 |
| preflight 拒绝 | 不变 | 不变 |

## 12. 测试矩阵

| 用例 | 预期 |
|---|---|
| 非 low 风险干预 × K，risk_quota=K | 前 K 次执行；第 K+1 次 prepare 后、execute 前停止（`budget.risk_quota`） |
| prepare 但被 single-change 拒绝 | 不执行、不计预算、`triggered_field="single_change_per_window"` |
| apply_started=True 后 rollback（无 Experiment） | 计干预/风险预算 |
| **apply 已开始后 execute 抛异常（无 Experiment）** | **据 receipt 计干预/风险预算 +1，并 fail-closed** |
| proposal 自报非 risky 但 manifest 项为 high | fail-closed（风险一致性校验拒绝） |
| 风险字段缺失 | 模型校验失败（无默认） |
| plan_digest 被伪造 | `InterventionOutcome.plan_digest != InterventionPlan.digest` → 校验失败 |
| 存量 fixture（无新字段） | 行为不变（legacy 兼容） |

## 13. 已锁定语义（D5-I1 实施）

1. preflight 在 apply 前拒绝：不计 interventions，也不计 risky_interventions。
2. `risk_quota` 耗尽复用 `GateStopClass.BUDGET_EXHAUSTED`，用 `triggered_field="budget.risk_quota"` 区分，不新增停类。
3. manifest 风险是下界：任务风险只能提高、不能降低；最终风险取二者较高值。缺失任务风险、缺失 manifest 绑定、绑定不一致全部 fail-closed。
4. 新建独立、版本化 `InterventionExecutionReceipt`，不复用临时 `intervention-failure` JSON；digest 可重算并绑定 `plan.digest`。
