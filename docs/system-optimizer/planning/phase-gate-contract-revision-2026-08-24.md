# phase-gate 合同修订设计 R3（两阶段干预接口 + 异常合同）

> 状态：draft（提案，等主 agent 确认后再编码）。**不写实现**。
> 关联：`phase_gate.py`、`dynamic_loop.py`、`hypothesis.py`、`dynamic_adapters.py`、
> `safety.py`、`config_manifest.py`（ConfigItem.risk / RiskLevel）。
> 日期：2026-08-24（R3 修订，取代 R2）。

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
    change_count: int                          # = len(change)，single-change 执行前检查用
    risk: RiskLevel                            # 显式必填，无默认
    risk_source: RiskSource                    # 风险来源与一致性证据

    @property
    def digest(self) -> str:                   # canonical digest，非可伪造字段
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))
```

- **不使用 `plan_digest` 字段**：`digest` 是 `@property` 计算值，攻击者无法自报一个与内容不一致的 plan_digest。
- `InterventionOutcome.plan_digest` 回绑 `plan.digest`（即 `InterventionPlan.digest` 的返回值），不信任任何自报字段。
- `risk` **必填**（RiskLevel 枚举，无默认）；缺失即模型校验失败，不静默降级为 low。
- `change_count` 由 `change` 派生（`len(change)`），single-change 在**这个**字段上执行前检查。

## 4. 风险来源与一致性（不信任 proposal 自报）

1. **manifest 基准**：`ConfigItem.risk`（low/medium/high）；plan 涉及的所有项取**最高者**为 manifest 基准风险。
2. **任务风险合同**：任务可显式声明 higher/lower，但必须给 `RiskSource` 证据，并与 manifest 基准**校验一致**——声明低于 manifest 最高风险时须给理由并记录；不一致 fail-closed。
3. **不得仅凭 proposal 自报 `risky=False` 绕过 manifest 风险**。

`RiskSource` 至少含：来源类型（manifest-derived / task-override）+ manifest 项列表 + 各项目 `RiskLevel` + 关联 manifest digest。

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

## 6. execute_intervention 异常合同

- **正常操作性失败**（apply 失败、verify 失败、业务复测拒绝、回退失败等）：在开始执行后**必须返回 `InterventionOutcome`**，不抛异常；`write_attempted`/`apply_started`/`rollback_*` 如实反映进展。
- **意外异常**（未预期的 bug/崩溃）：必须有**执行 receipt/journal** 记录 `apply_started` 至少落盘（复用 `SafetyController` 已有的 `control/intervention-failure-*.json` 通道，或新增 receipt 模型）；receipt 本身写不进时也要 best-effort 落盘并保留原始异常。
- **dynamic_loop 异常路径**：捕获 `execute_intervention` 的意外异常后，**据 receipt 计 intervention/risk**（`apply_started=True` 即计），随后 fail-closed（停止本相位并记录原始异常 + receipt digest）。不因异常丢失预算计数。

## 7. 预算计数口径

| 情况 | 计 intervention | 计 risk |
|---|---|---|
| 仅 prepare（proposed） | 否 | 否 |
| 执行前门禁拒绝（single-change / risk_quota） | 否 | 否 |
| `apply_started=True`（含 rollback / 无 Experiment / 意外异常） | **是** | 若 plan.risk 非 low 则**是** |
| 纯 preflight 拒绝（apply 之前） | **待用户确认** | 待用户确认 |

## 8. risk_quota 语义（执行前检查）

`risk_quota` = 「最多允许**执行**的 risky 干预数」。执行前检查（`execute_intervention` 之前）：

```text
if plan.risk != low and risky_interventions >= risk_quota:
    生成停止（不执行本次 plan）
```

判定用 `>=`；删除 `evaluate_phase_gate` 现存 `risky_interventions > risk_quota` 停类分支（由执行前检查接管）。

## 9. stop decision 的生成（不是抽象 stop()）

执行前门禁被拒时生成现有 `GateDecision`：

- `stop_class = GateStopClass.BUDGET_EXHAUSTED`；
- `triggered_field`：single-change 拒填 `"single_change_per_window"`，risk_quota 拒填 `"budget.risk_quota"`；
- `reason`：写明「下一次 risky 干预前 current >= quota（current=…, quota=…）」；
- `contract_digest` = `gate_contract.digest`；`evidence_digest` = 当前 `gate_state.evidence_digest`。

`run_dynamic_phase` 把该 `GateDecision` 写入 `DynamicPhaseRun.stop_gate_decision`。

## 10. schema 版本与 legacy 加载策略

- `InterventionPlan` / `InterventionOutcome` / `RiskSource` 为新 schema（`looper.*/v1alpha1`），只增不删。
- `InterventionOutcome.experiment` 保留旧 `InterventionExperiment` 子模型，`accepted`/`business_lcb` 语义不变。
- **新增必填字段不得静默改变历史 digest**：历史产物不加必填字段、不改 digest 口径；新字段全在新 schema。若未来给 `ComponentHypothesis` 加 risk，需评估对 `HypothesisLedger.digest` 的影响（沿用 `test_system_opt_optimization_run_versions.py` 的分派加载策略）。

## 11. 状态迁移表

| 事件 | interventions | risky_interventions |
|---|---|---|
| prepare 成功 | 不变 | 不变 |
| 执行前门禁拒绝 | 不变 | 不变 |
| apply_started=True（任何终态，含异常） | +1 | plan.risk!=low 时 +1 |
| preflight 拒绝 | 待确认 | 待确认 |

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

## 13. 待用户决定的问题（不在提案里选默认）

1. 纯 preflight 拒绝是否计入 intervention/risk 预算？
2. `risk_quota` 耗尽复用 `BUDGET_EXHAUSTED` 停类还是新开 `RISK_QUOTA` 停类？
3. 任务风险合同与 manifest 风险冲突时，取更高者还是必须显式 override + 记录？
4. 意外异常的执行 receipt 是否复用 `control/intervention-failure-*.json`，还是新增独立 receipt schema？
