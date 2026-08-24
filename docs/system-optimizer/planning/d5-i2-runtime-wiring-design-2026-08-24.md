# D5-I2 运行时接线设计：两阶段干预 + durable receipt（R1 修订稿）

> 状态：**R1（已解决 6 个架构闭环问题，待复审）**——上一版（`2abd1de`）保留为「现状审计 + 初版」。
> 基线：origin/system-optimizer-impl@39af89c（D5-I1 已接入为 0c69cdd + 87d776c）。
> 日期：2026-08-24。

本文只做设计冻结（不写生产代码），不自行决定风险阈值、wall-clock、retry 次数、receipt
保留期、attention 清除策略、真实 Linux 写入策略——这些列为任务显式输入或待确认。

## R1 变更记录（对应主 agent 阻断的 6 个问题）

| # | 问题 | R1 解决方案 |
|---|---|---|
| 1 | observer 引用 ReceiptStage 形成 safety↔intervention 循环依赖 | 在 `safety.py` 定义**中立** `SafetyProgressStage`/`SafetyProgressEvent`；`intervention.py` 提供 `SafetyProgressStage→ReceiptStage` 映射，adapter 调用映射（§3） |
| 2 | receipt 无前驱链/序号，pointer 丢失后重扫不可判最新 | receipt 增加 `sequence` + `predecessor_receipt_digest` + 分叉拒绝；pointer 丢失时按前驱链重建 head（§4） |
| 3 | 删 `evaluate_phase_gate` 风险分支破坏 v1alpha1 确定性回放 | 冻结 v1alpha1 evaluator，新增 v1alpha2 合同 + `evaluate_phase_gate_v2`（无风险后置分支），按 schema 分派（§7） |
| 4 | 业务拒绝后的恢复是第二次 execute(keep=True)，与同一 receipt 的 rollback 阶段冲突 | receipt 增加 `operation`（CANDIDATE/RECOVERY）作用域；恢复用独立 recovery receipt 链，经 `parent_receipt_digest` 关联（§4.3） |
| 5 | observer 异常契约无法用 SafetyResult 保留异常链 / 写回失败通道 | 前置用 typed `ProgressRecordError`（`raise ... from`）；后置用结构化 `SafetyResult.progress_failures` 累积，经独立 AttentionSink 持久化（§3.4） |
| 6 | attention 由 CLI 的 FileTargetGuard 持有，A/B 包无能力实现 | 定义中立 `AttentionSink` 协议；新增 D5-I2-C（CLI 集成）把 FileTargetGuard 注入为 sink（§8.3） |

---

## 1. 当前依赖流（逐节点事实）

链路（生产侧，经文件适配器）：

```
run_dynamic_phase (dynamic_loop.py:113)
  -> HypothesisLedger.request_intervention (hypothesis.py:174)
  -> intervention callback = SafetyBackedIntervention.__call__ (dynamic_adapters.py:492)
  -> SafetyController.execute (safety.py:109)
  -> backend snapshot/apply/verify/rollback (executor/__init__.py Protocol)
  -> business retest (BusinessRetestPlanner.judge + 外部 runner 补窗)
  -> InterventionExperiment (hypothesis.py:62)
  -> dynamic-loop interventions 计数 (dynamic_loop.py:270)
  -> evaluate_phase_gate (phase_gate.py:134)
```

### 1.1 `run_dynamic_phase`（dynamic_loop.py:113）
- 输出：`DynamicPhaseRun`。不直接写配置（写入只在 `intervention` 回调内）。
- 抛异常：`intervention(head)`（line 269）的异常**未捕获**，直接冒出；`HypothesisRoutingError`
  被捕获转 `PROBE_BLOCKED`。
- 异常时目标可能已改变：**可能**（回调内已 apply 后失败）。
- durable evidence：无版本化记录；`intervention-failure-*.json` 是临时产物。
- 计数准确性：**不准确**——`interventions += 1`（line 270）只在正常返回后执行；异常丢失；
  `risky_interventions` 只初始化（line 171）**从不递增**，`evaluate_phase_gate` 的 risk_quota
  分支在真实循环里永不触发。
- D5-I2 修改层：这里（prepare→gate→execute、risky 计数、异常按 receipt 计数）。

### 1.2 `HypothesisLedger.request_intervention`（hypothesis.py:174）
- D2 规则 1（≥2 竞争假设）；不写配置。D5-I2 不改。

### 1.3 `intervention` 回调 = `SafetyBackedIntervention.__call__`（dynamic_adapters.py:492）
- 输出 `InterventionExperiment | None`；写配置（`execute(..., keep=True)`）。
- 抛异常：restore 失败抛 `DynamicInterventionError`（line 554）；其余失败写临时
  `intervention-failure-*.json` 返回 `None`。
- D5-I2 修改层：这里（两阶段化，产出 `InterventionOutcome` + receipt）。

### 1.4 `SafetyController.execute`（safety.py:109）
- 输出 `SafetyResult`。写配置（apply@210、rollback@379）。
- snapshot 之后到 apply/verify/rollback **不捕获 backend 异常**（异常冒出，目标可能已改）。
- 无 durable progress 记录。
- D5-I2 修改层：这里（D5-I2-A 可选 progress observer）。

### 1.5 backend（executor/__init__.py:119-135）
- `ExecutorBackend` Protocol：`snapshot/apply/verify/rollback`。`apply/rollback` 写配置。
- D5-I2 不改。

### 1.6 business retest（`BusinessRetestPlanner.judge`，dynamic_adapters.py:433）
- 输出 `ImprovementEvidence`；不写配置；可能抛 `RetestIdentityDrift`/`SessionFileMissing`。
- D5-I2 不改（只读消费）。

### 1.7 `InterventionExperiment`（hypothesis.py:62）
- 纯数据。D5-I2 不改。

### 1.8 interventions 计数（dynamic_loop.py:270）
- 见 1.1。D5-I2 按 receipt/outcome 的 `apply_started` 计 interventions，按
  `resolved.final_risk != low` 计 risky。

### 1.9 `evaluate_phase_gate`（phase_gate.py:134）
- 判定顺序固定；risk_quota 分支 `risky_interventions > risk_quota`（line 181，严格 `>`）。
- D5-I2 修改层：v1alpha2 语义（§7），**v1alpha1 冻结**。

---

## 2. 核心设计一：prepare → gate → execute

### 2.1 接口签名（供 D5-I2-B 实现）

```python
def prepare_intervention(
    *, hypothesis: ComponentHypothesis, proposal: HypothesisProposal,
    manifest: ConfigManifest, task_risk: RiskLevel,
    risk_kind: RiskSourceKind, risk_rationale: str | None = None,
) -> InterventionPlan

def evaluate_intervention_gate(
    *, plan: InterventionPlan, manifest: ConfigManifest,
    contract: DynamicPhaseGateContract, risky_interventions: int,
    evidence_digest: str,
) -> GateDecision | None      # D5-I1 已落地

def execute_intervention(
    *, plan: InterventionPlan, receipt: InterventionExecutionReceipt,
    store: DurableReceiptStore, controller: SafetyController,
    manifest: ConfigManifest, backend: ExecutorBackend,
    fencing_token: int, planner: BusinessRetestPlanner,
    attention: AttentionSink,
) -> InterventionOutcome
```

### 2.2 语义
- manifest 由会话侧注入（`SafetyBackedIntervention` 现持有 `self._manifest`，dynamic_adapters.py:479）。
- `change` = `proposal.change`；`RiskSource` 由 `prepare_intervention` 派生（按 `item_id` 升序）。
- `task_risk` 任务显式输入，**不得默认 low**；缺 task risk 时新执行路径 fail-closed。
- prepare 不写配置；single-change/risk-quota 在 execute 前由 gate 完成；gate 拒时不计预算、
  不创建「执行已开始」证据。

---

## 3. 核心设计二：L1 进度观察缝（D5-I2-A）——R1 修订

### 3.1 里程碑定位（不变，事实）

| SafetyProgressStage | SafetyController 位置 |
|---|---|
| PREFLIGHT_COMPLETED | safety.py:173 `event(PREFLIGHT, "preflight", "succeeded")` |
| APPLY_STARTED | safety.py:210 第一次 `backend.apply` 之前 |
| ROLLBACK_STARTED | safety.py:379 第一次 `backend.rollback` 之前（`_rollback`） |
| ROLLBACK_VERIFIED | safety.py:396 rollback verify + safety.py:414 round-trip 匹配后 |
| TERMINAL | KEPT@286 / ROLLED_BACK@428 / NEEDS_ATTENTION@428 / REJECTED@162/187 |

### 3.2 消除循环依赖（问题 1）

`SafetyController.execute` **不 import `intervention.py`**。中立类型定义在 `safety.py`：

```python
# safety.py（不 import intervention.py）
class SafetyProgressStage(StrEnum):
    PREFLIGHT_COMPLETED = "preflight-completed"
    APPLY_STARTED = "apply-started"
    ROLLBACK_STARTED = "rollback-started"
    ROLLBACK_VERIFIED = "rollback-verified"
    TERMINAL = "terminal"

class SafetyProgressEvent(StrictModel):
    stage: SafetyProgressStage
    safety_state: SafetyState
    item_id: str | None = None
    operation: str | None = None

ProgressObserver = Callable[[SafetyProgressEvent], None]
```

`SafetyController.execute(..., progress_observer: ProgressObserver | None = None)`。

映射放在 `intervention.py`（其已 `from ...safety import SafetyState`，同向无环）：

```python
# intervention.py
def receipt_stage_for(stage: SafetyProgressStage) -> ReceiptStage | None:
    # PREFLIGHT_COMPLETED / TERMINAL -> None（只注解 safety_state，不推进 receipt）
    # APPLY_STARTED -> ReceiptStage.APPLY_STARTED
    # ROLLBACK_STARTED -> ReceiptStage.ROLLBACK_ATTEMPTED
    # ROLLBACK_VERIFIED -> ReceiptStage.ROLLBACK_VERIFIED
```

依赖方向：`safety.py`（不依赖 intervention）→ `intervention.py`（依赖 safety）→
`intervention_receipt.py`（依赖两者）。无环。

### 3.3 observer 异常契约（问题 5）——typed 异常 + 结构化累积

- **apply 前**（APPLY_STARTED 里程碑）observer 抛异常：`SafetyController.execute` 抛
  `ProgressRecordError`（`raise ProgressRecordError(...) from observer_error`），**不执行
  `backend.apply`**。调用方（execute_intervention）捕获后产出 `InterventionOutcome(
  write_attempted=False, apply_started=False, ...)`——不尝试把 Python 异常对象塞进 SafetyResult。
- **apply 后**（ROLLBACK_STARTED/ROLLBACK_VERIFIED/TERMINAL）observer 抛异常：**不中断补偿**，
  把结构化错误累积进 `SafetyResult.progress_failures: list[SafetyProgressFailure]`：

```python
class SafetyProgressFailure(StrictModel):
    stage: SafetyProgressStage
    error_type: str
    error_message: str
```

- 后置失败的持久化**不走失败的回执通道**：由调用方经独立 `AttentionSink`（§8.3）写
  needs-attention，或 best-effort 记录后显式声明「未能记录」。
- `SafetyController` 需要 `import` 的是 `safety.py` 自身的 `ProgressRecordError`（同文件），
  无跨模块异常链问题。

### 3.4 兼容性

`progress_observer=None` 时行为与现状完全一致；现有调用方签名不变。

---

## 4. 核心设计三：durable receipt store（D5-I2-A）——R1 修订

### 4.1 落盘布局

- 内容寻址：`receipts/<receipt_digest_hex>.json`（`receipt_digest_hex = receipt.digest.removeprefix("sha256:")`）。
- current pointer：`receipts/<plan_digest_hex>.current.json` = `{"plan_digest", "head_receipt_digest"}`。
- 原子写顺序：先写内容寻址 receipt 文件，再原子更新 pointer（tmp + `os.replace`，复用
  lease.py `FileTargetGuard._atomic_write` 模式，lease.py:156）。

### 4.2 单调可恢复日志（问题 2）

`InterventionExecutionReceipt`（intervention.py，D5-I2-A 增字段；**尚无已持久化数据**，
D5-I1 明确不落盘，故可安全增字段）：

```python
class ReceiptOperation(StrEnum):
    CANDIDATE = "candidate"
    RECOVERY = "recovery"

class InterventionExecutionReceipt(StrictModel):
    ...                      # D5-I1 既有字段
    sequence: int = Field(ge=0)                    # 同 operation 链内单调递增
    predecessor_receipt_digest: str | None = Field(default=None, pattern=_DIGEST)
    operation: ReceiptOperation = ReceiptOperation.CANDIDATE
    parent_receipt_digest: str | None = Field(default=None, pattern=_DIGEST)  # RECOVERY 关联 CANDIDATE 终态
```

链不变量（store 强制）：
- `sequence == predecessor.sequence + 1`；`predecessor_receipt_digest == predecessor.digest`。
- **分叉拒绝**：同一 `(plan_digest, operation, stage)` 出现不同 digest → fail-closed；同一
  `sequence` 出现两个不同 head → fail-closed。
- 同阶段重放（相同内容）幂等；旧 stage 重放（倒退）拒绝。

**pointer 丢失时的重扫**（现在成立）：沿 `predecessor_receipt_digest` 回溯重建链，head =
「未被任何 receipt 引为 predecessor」的那条；若有多个 head 或链断开 → fail-closed（不再
承诺静默选一）。`RECOVERY` 链经 `parent_receipt_digest` 关联候选链，两条链各自单调、
互不跨越阶段。

### 4.3 业务拒绝恢复的作用域（问题 4）

- 候选执行（keep 路径）= 一条 `CANDIDATE` receipt 链。
- 业务拒绝后的恢复是**第二次** `SafetyController.execute(... keep=True)`（dynamic_adapters.py:545），
  在 L1 是新的 apply/KEPT，**不是** rollback。因此它产生一条**独立的 `RECOVERY` receipt 链**，
  `parent_receipt_digest` 指向候选链终态 receipt。
- 候选链的 `rollback_*` 阶段只描述**单次 execute 内的 L1 回退**（verify 失败路径），
  绝不描述业务恢复；两条链各自的 `advance` 单调，不会出现「apply_started 上报倒退」。

### 4.4 失败语义

- 文件缺失 / 悬空 pointer / 篡改（digest 与内容不符、plan_digest 不匹配）/ 倒退 / 分叉 /
  链断开 → fail-closed。
- receipt 写失败保留原始异常：`raise ... from original`；`error` 字段记录 best-effort。
- 真实性边界：自洽 ≠ 真实；可信任锚是 manifest digest / 外部签名。不加签名实现。

### 4.5 API

```python
class DurableReceiptStore:
    def __init__(self, root: Path) -> None: ...
    def advance(self, plan_digest: str, operation: ReceiptOperation,
                stage: ReceiptStage, **fields) -> InterventionExecutionReceipt: ...  # 建链/推进 + 原子落盘 + 单调 pointer
    def head(self, plan_digest: str, operation: ReceiptOperation) -> InterventionExecutionReceipt | None: ...
    def verify_chain(self, plan_digest: str, operation: ReceiptOperation) -> InterventionExecutionReceipt: ...  # fail-closed 重建
```

---

## 5. 核心设计四：预算计数状态机（R1 修订）

`risky` 判定统一用 `resolved.final_risk != low`。

| 事件 | interventions +1? | risky +1? | 停止相位? | stop class / triggered_field | attention? | 证据 digest |
|---|---|---|---|---|---|---|
| prepare 成功 | 否 | 否 | 否 | — | 否 | plan.digest |
| gate single-change 拒绝 | 否 | 否 | 是 | BUDGET_EXHAUSTED / `single_change_per_window` | 否 | 窗口 evidence |
| gate risk-quota 拒绝 | 否 | 否 | 是 | BUDGET_EXHAUSTED / `budget.risk_quota` | 否 | 窗口 evidence |
| L1 preflight 拒绝 | 否 | 否 | 否 | — | 否 | outcome.evidence_digest |
| apply_started 后 kept | 是 | 若 final_risk!=low | 否 | — | 否 | outcome.evidence_digest |
| apply_started 后 verify 失败→L1 rollback | 是 | 若 final_risk!=low | 否 | — | 否 | outcome.evidence_digest |
| L1 rollback 失败 | 是 | 若 final_risk!=low | 是（fail-closed） | 见待确认 | **是** | receipt(CANDIDATE).digest |
| business retest 拒绝→恢复成功 | 是 | 若 final_risk!=low | 否 | — | 否 | experiment.measurement_batch_digest + receipt(RECOVERY).digest |
| 恢复（RECOVERY execute）失败 | 是（候选已计） | 同上 | 是（fail-closed） | 见待确认 | **是** | receipt(RECOVERY).digest |
| execute 意外异常但 receipt 显示 apply_started | 是 | 若 final_risk!=low | 是（fail-closed） | 见待确认 | **是**（目标未知） | receipt.digest |
| receipt 写失败 | 视时机 | 视时机 | 见 3.3 | — | 是（apply 后） | receipt/attention |

- 计数基于 `execute_intervention` 产出的 `InterventionOutcome`（或异常路径 receipt）的
  `apply_started`，不基于 `intervention()` 返回非 None。
- 异常路径：loop 捕获后按 receipt `apply_started` 计 interventions/risky，fail-closed 停相位，
  保留原始异常 + receipt digest。

---

## 6. 核心设计五：dynamic-loop schema/versioning（不变）

- `DynamicPhaseRun`（`looper.dynamic-phase-run/v1alpha1`）需新增 `risky_interventions`、
  `execution_receipts`，**不能直接改 v1alpha1**（digest 用 `exclude_none=False`）→ 升
  `v1alpha2` + legacy 分派（沿用 `test_system_opt_optimization_run_versions.py` 模式）。
- `DynamicWindowRecord` 增 `plan_digest/outcome_digest/receipt_digest`（可选）。
- `WindowAction`（StrEnum）可安全增员：建议 `GATE_REJECTED`、`INTERVENTION_FAILED`。
- `HypothesisProposal` 增 `risk/risk_kind/risk_rationale`；`HypothesisProposalsFile` 升
  `v1alpha2`，risk 无默认。
- 缺 task risk：legacy 路径只读，新执行路径 fail-closed，均不默认 low。

---

## 7. 核心设计六：phase-gate evaluator 版本化（问题 3）——R1 修订

- **v1alpha1 冻结**：`evaluate_phase_gate` + `DynamicPhaseGateContract`
  （`looper.dynamic-phase-gate/v1alpha1`，含 `risky_interventions > risk_quota` 后置分支）
  **逐字节保持**，历史合同与状态回放结果不变。
- **新增 v1alpha2 语义**：`DynamicPhaseGateContractV2`（`looper.dynamic-phase-gate/v1alpha2`）
  + `evaluate_phase_gate_v2`——判定顺序不变，但**移除 risk_quota 后置分支**（风险配额改为
  执行前 `evaluate_intervention_gate` 的 `>=` 语义独占）。
- 分派：会话 `gate-contract.json` 的 `schema_version` 决定用哪个 evaluator；`run_dynamic_phase`
  接线后接受 v1alpha2 合同 + `evaluate_phase_gate_v2`。
- 确定性回放：v1alpha1 合同 → v1alpha1 evaluator（行为不变）；v1alpha2 合同 → v2 evaluator
  （新语义）。二者并存，无版本漂移。

---

## 8. 实施拆包（三个互不混写的包）——R1 修订

### 8.1 D5-I2-A：L1 progress observer + durable receipt log（**必须先完成**）

- 依赖：D5-I1（0c69cdd + 87d776c）。
- 写集合：`safety.py`（`SafetyProgressStage/Event`、`ProgressRecordError`、可选
  `progress_observer`、`SafetyResult.progress_failures`）、`intervention.py`（receipt 增
  `sequence/predecessor_receipt_digest/operation/parent_receipt_digest` + `receipt_stage_for`
  映射 + `ReceiptOperation`）、新增 `intervention_receipt.py`（`DurableReceiptStore` +
  `AttentionSink` 协议）、对应单测。
- 不可改：`dynamic_loop.py`、`dynamic_adapters.py`、`phase_gate.py`、`cli.py`、collection/replay、
  rollback/regression、executor backends、GPT 测试。
- API：§3.2 / §4.5；`AttentionSink` 协议（§8.3）。
- 验收：observer 5 里程碑被调用；apply 前失败抛 `ProgressRecordError` 且不 apply；apply 后
  失败累积 `progress_failures` 且继续 rollback；receipt 前驱链/序号/分叉/倒退 fail-closed；
  pointer 丢失重扫可重建 head；`progress_observer=None` 全量行为不变。

### 8.2 D5-I2-B：dynamic adapters + dynamic loop + gate/count 接线（依赖 A）

- 依赖：D5-I2-A。
- 写集合：`dynamic_adapters.py`（`SafetyBackedIntervention` 两阶段化 → `prepare_intervention`
  / `execute_intervention`，接 store + `AttentionSink`）、`dynamic_loop.py`（prepare→gate→execute、
  risky 计数、异常按 receipt 计数 + fail-closed）、`phase_gate.py`（新增
  `DynamicPhaseGateContractV2` + `evaluate_phase_gate_v2`，**v1alpha1 不动**）、schema/versioning
  v1alpha2 分派、对应测试。
- 不可改：`safety.py`（A 完成）、`cli.py`、executor backends、collection/replay、rollback。
- 验收：§9 全矩阵；legacy fixture digest 不变；simulated E2E 正向不回归。

### 8.3 D5-I2-C：CLI attention sink 集成（依赖 B，**闭合问题 6**）

- 依赖：D5-I2-B。
- 写集合：`services/api/looper_api/cli.py`（把 `FileTargetGuard.mark_needs_attention` 注入为
  `AttentionSink` 具体实现；动态 run 的 receipt-attention 路径接线）、services 层胶水、对应测试。
- 不可改：core 包内 L0/L1 模块（`safety.py`/`intervention.py`/`intervention_receipt.py` 等在
  A/B 已定）、executor backends、collection/replay、rollback、GPT 测试。
- 中立协议（定义在 A，供 B/C 共同依赖，避免 CLI 层反向依赖）：

```python
class AttentionSink(Protocol):
    def __call__(self, *, target_id: str, reason: str, evidence_digest: str) -> None: ...
```

- 验收：receipt apply 后写失败但恢复成功 → 仍经 sink 标 needs-attention；候选/恢复异常 →
  sink 落 attention；`FileTargetGuard` 现有语义不变。

### 8.4 并行性

A → B → C 顺序（链式依赖）。A 内：`DurableReceiptStore`（纯）与 observer 钩子可并行；
B 内：`phase_gate` v2 evaluator 与 adapter/loop 接线弱耦合，可在 B 验收后收尾；C 独立于 A/B
的实现细节，只依赖接口。

---

## 9. 测试矩阵（验收覆盖，R1 增补）

| 用例 | 预期 |
|---|---|
| low/medium/high manifest 风险 | 解析正确（D5-I1-R1 已测，接线复验） |
| task override（提高 + rationale） | 通过，final_risk 提高 |
| single-change 拒绝 | 零写、不计预算、`single_change_per_window` |
| quota=K：前 K 次执行、第 K+1 次执行前停 | `evaluate_intervention_gate` 拒 |
| preflight 拒绝 | 不计数、无 apply |
| apply_started 后无 Experiment | 仍计 interventions/risky |
| apply 异常 | 据 receipt 计数 + fail-closed + 保留原异常 |
| verify 失败 | L1 rollback + 计数 + 无 attention（rollback_verified） |
| rollback 成功 / 失败 | 成功不 attention；失败 attention + fail-closed |
| business retest 拒绝→恢复 | 候选链 + 独立 RECOVERY 链，各单调、`parent_receipt_digest` 关联 |
| receipt 写失败（apply 前 / 后） | 前：`ProgressRecordError` 不 apply；后：继续补偿 + attention |
| observer 抛异常（前 / 后） | 前：typed 异常不 apply；后：`progress_failures` 累积不中断补偿 |
| receipt 分叉 / 倒退 / 链断开 / 悬空 pointer | fail-closed |
| pointer 丢失重扫 | 沿前驱链重建唯一 head |
| outcome/receipt/plan digest 不一致 | `verify_outcome_binding` / store fail-closed |
| lease 最终释放 | kept/rollback/异常/恢复 后 `FileTargetGuard.release` 最终调用 |
| 原始异常上下文不丢 | `raise ... from` / 结构化 error 记录 |
| v1alpha1 确定性回放 | 旧合同+旧 state 结果不变 |
| legacy fixture digest 不变 | v1alpha1 fixture 加载 digest 口径不变 |
| simulated E2E 正向不回归 | `test_system_opt_dynamic_e2e.py` 全绿 |
| attention sink 注入 | apply 后写失败但恢复成功仍标 needs-attention |

---

## 10. 禁止默认（只能列为任务输入或待确认）

风险阈值、wall-clock 数值、retry 次数、receipt 保留期（GC）、attention 自动清除策略、真实
Linux 写入策略——不得自选，列为任务显式输入或待确认。

---

## 11. 待确认问题（R1 收窄后）

1. `tests/test_system_opt_dynamic_adapters.py` 不存在（`SafetyBackedIntervention` 由
   `tests/test_system_opt_dynamic_e2e.py` 测试）；D5-I2-B 的 adapter 测试放新文件还是并入
   `dynamic_e2e.py`？
2. `rollback 失败` / `execute 意外异常但 apply_started` / `恢复失败` 的停止语义：复用
   `SAFETY_TRIGGERED/degradation` 还是新增 stop class 或专用 triggered_field（本文不新增停类，
   需确认）。
3. `write_attempted` 与 `apply_started` 当前同缝，是否接受（等未来 backend 引入独立写步骤再拆）。
4. current pointer 方案（`<plan_hex>.current.json`）与纯前驱链重扫是否需要同时保留
   （本文两者都保留：pointer 快路径 + 链重建兜底，需确认）。
5. `InterventionExecutionReceipt` 增字段就地改 v1alpha1 还是升 v1alpha2（本文建议就地增，
   因 D5-I1 从未落盘、无历史 receipt digest，需确认）。
6. 缺 task risk 旧 proposal 只读 vs fail-closed（本文建议 fail-closed + 只读兼容，需确认）。

---

## 12. 引用校验记录

下列符号/文件已在 `origin/system-optimizer-impl@39af89c` 确认存在（本文引用其名与关键行号）：

- `dynamic_loop.py`：`run_dynamic_phase`(113)、`WindowAction`(79)、`DynamicWindowRecord`(88)、
  `DynamicPhaseRun`(97)、`interventions += 1`(270)、`risky_interventions=0` 初始化(171)。
- `hypothesis.py`：`HypothesisLedger.request_intervention`(174)、`InterventionExperiment`(62)、
  `HypothesisRoutingError`(34)。
- `dynamic_adapters.py`：`SafetyBackedIntervention`(456)、`__call__`(492)、
  `DynamicInterventionError`(88)、`HypothesisProposal`(143)、`HypothesisProposalsFile`(154)、
  `BusinessRetestPlanner.judge`(433)、恢复 `execute(...keep=True)`(545)。
- `safety.py`：`SafetyController.execute`(109)、preflight succeeded(173)、`backend.apply`(210)、
  `backend.rollback`(379)、rollback verify(396)、round-trip(414)、KEPT(286)、NEEDS_ATTENTION(428)、
  ROLLED_BACK(428)。
- `phase_gate.py`：`evaluate_phase_gate`(134)、`risky_interventions > risk_quota`(181)、
  `DynamicPhaseGateContract`(74)、`GateDecision`(108)、`GateStopClass`(30)、`PhaseBudget`(54)。
- `executor/__init__.py`：`ExecutorBackend`(119)、`ConfigSnapshot`(74)、`OperationResult`(89)、
  `OperationStatus`(25)。
- `lease.py`：`FileTargetGuard`(117)、`TargetLease`(28)、`TargetAttention`(41)、`_atomic_write`(156)、
  `mark_needs_attention`(238)、`acquire`(180)、`release`(227)。
- `intervention.py`（D5-I1-R1）：`InterventionPlan/InterventionOutcome/InterventionExecutionReceipt/
  ReceiptStage/RiskSource/RiskSourceKind/ResolvedPlanRisk` + `resolve_plan_risk`、
  `evaluate_intervention_gate`、`verify_outcome_binding`、`InterventionContractError`。
- `services/api/looper_api/cli.py`：`FileTargetGuard`(70)、`mark_needs_attention`(369/634/883/
  1132/1530)、`clear_attention`(740)、`_mark_regression_attention`(360)。
- 测试：`test_system_opt_dynamic_loop.py`、`test_system_opt_dynamic_e2e.py`、
  `test_system_opt_safety.py`、`test_system_opt_intervention.py`、
  `test_system_opt_optimization_run_versions.py` 均存在。

**注意**：任务「可只读」清单中的 `tests/test_system_opt_dynamic_adapters.py` **不存在**；
`SafetyBackedIntervention` 真实测试在 `tests/test_system_opt_dynamic_e2e.py`（§11.1）。
