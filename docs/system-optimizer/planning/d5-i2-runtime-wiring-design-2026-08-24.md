# D5-I2 运行时接线设计：两阶段干预 + durable receipt（R2 修订稿）

> 状态：**R2（已解决 R1 遗留 6 个实施阻断，待复审）**——R1（`409c1b6`）保留为修订草案。
> 基线：origin/system-optimizer-impl@39af89c（D5-I1 = 0c69cdd + 87d776c）。
> 日期：2026-08-24。

本文只做设计冻结（不写生产代码），不自行决定风险阈值、wall-clock、retry 次数、receipt
保留期、attention 清除策略、真实 Linux 写入策略——列为任务显式输入或待确认。

## R2 变更记录（对应主 agent 阻断的 6 个问题）

| # | 问题 | R2 解决方案 |
|---|---|---|
| 1 | CANDIDATE/RECOVERY 共用一个 `current.json` 互相覆盖 | pointer 按 `(plan_digest, operation)` 命名：`<plan>.candidate.current.json` / `<plan>.recovery.current.json`（§4.1） |
| 2 | 同阶段分叉规则与 PREFLIGHT/TERMINAL 字段更新矛盾 | receipt 升 v1alpha2 用**显式 stage**，新增 `PREFLIGHT_COMPLETED`/`TERMINAL`；每次推进 stage 严格上升，分叉检测改为「每个 predecessor 至多一个 successor」（§4.2） |
| 3 | backend 异常后不捕获、不补偿 | D5-I2-A 在 `_execute` 核心对 apply/verify/rollback 异常 `try/except` 捕获并路由补偿/NEEDS_ATTENTION，转成结构化 `SafetyResult`（§3.3） |
| 4 | AttentionSink 缺 target_id 来源 | 协议改为**无 target_id**：`Callable[[reason, evidence_digest], None]`；D5-I2-C 用 `backend.capabilities.target_id` 绑定（§8.3） |
| 5 | ProgressRecordError 转返回值后异常链丢失 | 前置统一「持久化失败证据后**重新抛出**」；后置走结构化结果、**取消异常链声明**（§3.4） |
| 6 | SafetyResult 加字段破坏兼容 + receipt 就地改 v1alpha1 | `SafetyResult` 冻结；新增 `execute_observed()` → `ObservedSafetyResult` 信封；receipt 升 **v1alpha2**（v1alpha1 冻结）（§3.5 / §4.2） |

---

## 1. 当前依赖流（逐节点事实，不变）

```
run_dynamic_phase (dynamic_loop.py:113)
  -> HypothesisLedger.request_intervention (hypothesis.py:174)
  -> intervention callback = SafetyBackedIntervention.__call__ (dynamic_adapters.py:492)
  -> SafetyController.execute (safety.py:109)
  -> backend snapshot/apply/verify/rollback (executor/__init__.py Protocol)
  -> business retest (BusinessRetestPlanner.judge + 外部 runner)
  -> InterventionExperiment (hypothesis.py:62)
  -> dynamic-loop interventions 计数 (dynamic_loop.py:270)
  -> evaluate_phase_gate (phase_gate.py:134)
```

- `run_dynamic_phase`（dynamic_loop.py:113）：`intervention(head)`（269）异常**不捕获**；
  `interventions += 1`（270）只在正常返回后执行（异常丢失）；`risky_interventions` 只初始化
  （171）从不递增。
- `HypothesisLedger.request_intervention`（hypothesis.py:174）：D2 规则 1，不写配置。
- `SafetyBackedIntervention.__call__`（dynamic_adapters.py:492）：输出 `InterventionExperiment
  | None`；恢复（第二次 `execute(..., keep=True)`，dynamic_adapters.py:545）失败抛
  `DynamicInterventionError`（554）；临时 `intervention-failure-*.json` 非版本化。
- `SafetyController.execute`（safety.py:109）：preflight succeeded@173、首次 `backend.apply`@210、
  首次 `backend.rollback`@379、rollback verify@396、round-trip@414、KEPT@286、NEEDS_ATTENTION@428、
  ROLLED_BACK@428；**snapshot 之后 backend 异常不捕获**。
- backend（executor/__init__.py:119-135）：`snapshot/apply/verify/rollback`，`apply/rollback` 写配置。
- `evaluate_phase_gate`（phase_gate.py:134）：`risky_interventions > risk_quota`@181（严格 `>`）。

---

## 2. 核心设计一：prepare → gate → execute

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
) -> GateDecision | None          # D5-I1 已落地

def execute_intervention(
    *, plan: InterventionPlan, receipt: InterventionExecutionReceiptV2,
    store: DurableReceiptStore, controller: SafetyController,
    manifest: ConfigManifest, backend: ExecutorBackend,
    fencing_token: int, planner: BusinessRetestPlanner,
    attention: AttentionSink,
) -> InterventionOutcome
```

- manifest 由会话侧注入（`SafetyBackedIntervention` 现持有 `self._manifest`，dynamic_adapters.py:479）。
- `change = proposal.change`；`RiskSource` 由 prepare 派生（按 `item_id` 升序）。
- `task_risk` 任务显式输入，缺 task risk 时新执行路径 fail-closed，不默认 low。
- prepare 不写配置；single-change/risk-quota 在 execute 前由 gate 完成；gate 拒时不计预算、
  不创建「执行已开始」证据。
- `target_id` 不在 `execute_intervention` 签名里；需要时（attention/lease）从
  `backend.capabilities.target_id`（executor/__init__.py:36）取，见 §8.3。

---

## 3. 核心设计二：L1 进度观察缝（D5-I2-A）——R2 修订

### 3.1 里程碑定位（不变）

| SafetyProgressStage | SafetyController 位置 |
|---|---|
| PREFLIGHT_COMPLETED | safety.py:173 |
| APPLY_STARTED | safety.py:210 第一次 `backend.apply` 之前 |
| ROLLBACK_STARTED | safety.py:379 第一次 `backend.rollback` 之前 |
| ROLLBACK_VERIFIED | safety.py:396 + safety.py:414 |
| TERMINAL | KEPT@286 / ROLLED_BACK@428 / NEEDS_ATTENTION@428 / REJECTED@162/187 |

### 3.2 中立类型（消除循环依赖，不变）

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
```

`intervention.py` 提供映射 `receipt_stage_for(stage: SafetyProgressStage) -> ReceiptStageV2`
（五档全映射，无 None，见 §4.2）。依赖方向 `safety ← intervention ← intervention_receipt` 无环。

### 3.3 backend 异常补偿（问题 3）

D5-I2-A 在 `SafetyController` 的**私有 `_execute` 核心**（供 `execute` 与 `execute_observed`
共用）对 backend 异常做 `try/except` 补偿：

- `backend.apply(...)` 抛异常 → 该 item **视为可能已修改**（`applied.append(item.id)`），
  立即进入 `_rollback(... reason=f"apply raised: {e}")`。
- `backend.verify(...)` 抛异常 → `_rollback`。
- `backend.rollback(...)` 抛异常 → `rollback_failed=True` → `SafetyState.NEEDS_ATTENTION`。
- 异常以结构化 `SafetyEvent`（operation + status + message=str(e)）记录，**不**把 Python
  异常对象塞进模型。

语义说明：这是把「backend 异常冒出 execute」改为「补偿 + 结构化结果」的**有意行为变更**
（目标可能已被修改，fail-closed 优先）。`SafetyResult` 模型字段不变（§3.5）。需审计的既有
调用方：`SafetyBackedIntervention`、`engine/loop.py`、`rollback/regression.py`、safety 测试。

### 3.4 observer 异常契约（问题 5）——二选一，取消「返回后保留链」

- **前置**（APPLY_STARTED 里程碑）observer 抛异常：`execute_observed` 以
  `raise ProgressRecordError(...) from observer_error` **重新抛出**，**不转成返回值**；不执行
  `backend.apply`。调用方（`execute_intervention`）**不捕获转 Outcome**，让其冒到 dynamic
  loop 的异常路径（据 receipt + fail-closed），异常链经 `raise ... from` 全程保留。
- **后置**（ROLLBACK_STARTED/ROLLBACK_VERIFIED/TERMINAL）observer 抛异常：**不中断补偿**，
  由 `execute_observed` 捕获并累积为结构化 `ObservedSafetyResult.progress_failures`；这里
  **明确取消「保留异常链」声明**——后置失败是结构化结果，不承诺 Python 异常链。

### 3.5 兼容性（问题 6a）——冻结 SafetyResult，新增信封

- `SafetyController.execute(...)` 签名与 `SafetyResult` **逐字段冻结**（不新增字段，序列化
  与摘要消费者不变）。
- 新增入口 `SafetyController.execute_observed(..., progress_observer) -> ObservedSafetyResult`：

```python
class SafetyProgressFailure(StrictModel):
    stage: SafetyProgressStage
    error_type: str
    error_message: str

class ObservedSafetyResult(StrictModel):
    schema_version: Literal["looper.observed-safety-result/v1alpha1"]
    result: SafetyResult                     # 未改动的内层结果
    progress_failures: list[SafetyProgressFailure] = Field(default_factory=list)
```

- 只有 `execute_observed` 走 observer/补偿信封；`execute`（无 observer）路径与现状一致，
  唯一差异是 §3.3 的 backend 异常补偿（安全修复，对两者都生效）。

---

## 4. 核心设计三：durable receipt store（D5-I2-A）——R2 修订

### 4.1 落盘布局与 pointer 命名（问题 1）

- 内容寻址：`receipts/<receipt_digest_hex>.json`。
- **pointer 按 operation 分离**：
  - `receipts/<plan_digest_hex>.candidate.current.json`
  - `receipts/<plan_digest_hex>.recovery.current.json`
  每条操作链独立 pointer，互不覆盖。
- 原子写顺序：先写内容寻址 receipt 文件，再原子更新对应 operation 的 pointer（tmp +
  `os.replace`，复用 lease.py `FileTargetGuard._atomic_write` 模式，lease.py:156）。

### 4.2 receipt v1alpha2：显式 stage + 单调日志（问题 2 / 6b）

`InterventionExecutionReceipt`（`looper.intervention-execution-receipt/v1alpha1`，D5-I1）
**冻结不动**。新增 `InterventionExecutionReceiptV2`
（`looper.intervention-execution-receipt/v1alpha2`）：

```python
class ReceiptStageV2(StrEnum):
    PLANNED = "planned"
    PREFLIGHT_COMPLETED = "preflight-completed"
    APPLY_STARTED = "apply-started"
    ROLLBACK_ATTEMPTED = "rollback-attempted"
    ROLLBACK_VERIFIED = "rollback-verified"
    TERMINAL = "terminal"

class ReceiptOperation(StrEnum):
    CANDIDATE = "candidate"
    RECOVERY = "recovery"

class InterventionExecutionReceiptV2(StrictModel):
    schema_version: Literal["looper.intervention-execution-receipt/v1alpha2"]
    plan_digest: str = Field(pattern=_DIGEST)
    operation: ReceiptOperation
    stage: ReceiptStageV2                     # 显式，非从 flag 派生
    sequence: int = Field(ge=0)
    predecessor_receipt_digest: str | None = Field(default=None, pattern=_DIGEST)
    parent_receipt_digest: str | None = Field(default=None, pattern=_DIGEST)  # RECOVERY 关联 CANDIDATE 终态
    safety_state: SafetyState | None = None
    evidence_digest: str | None = Field(default=None, pattern=_DIGEST)
    experiment: InterventionExperiment | None = None
    error: str | None = Field(default=None, min_length=1, max_length=2000)

    @property
    def digest(self) -> str: ...             # canonical_digest(model_dump(exclude_none=False))
```

- 映射 `receipt_stage_for`：`PREFLIGHT_COMPLETED→PREFLIGHT_COMPLETED`、`APPLY_STARTED→
  APPLY_STARTED`、`ROLLBACK_STARTED→ROLLBACK_ATTEMPTED`、`ROLLBACK_VERIFIED→ROLLBACK_VERIFIED`、
  `TERMINAL→TERMINAL`。**每个观察里程碑对应一个更高 stage**，推进严格上升，**不存在同 stage
  不同 digest 的合法更新**（问题 2 消除）。
- **分叉检测 = 每个 predecessor 至多一个 successor**：两条不同 digest 的 receipt 声称同一
  `predecessor_receipt_digest` → fail-closed；stage 单调（successor.stage rank ≥ predecessor）。
- 链不变量：`sequence == predecessor.sequence + 1`；`predecessor_receipt_digest == predecessor.digest`。
- **RECOVERY 作用域（问题 4 背景）**：候选执行 = 一条 CANDIDATE 链；业务拒绝后的恢复是
  第二次 `execute(keep=True)`，产生独立 RECOVERY 链，`parent_receipt_digest` 指向候选终态。
  两条链各自单调，`apply_started` 上报不会倒退。
- pointer 丢失重扫：沿 `predecessor_receipt_digest` 回溯重建 head（= 未被引为 predecessor
  的那条）；多头 / 链断开 → fail-closed。

### 4.3 失败语义

- 缺失 / 悬空 pointer / 篡改 / 倒退 / 分叉 / 链断开 → fail-closed。
- receipt 写失败保留原始异常：前置重新抛出（`raise ... from`）；后置走结构化累积，不声明
  异常链（§3.4）。
- 真实性边界：自洽 ≠ 真实；可信任锚是 manifest digest / 外部签名；不加签名实现。

### 4.4 API

```python
class DurableReceiptStore:
    def __init__(self, root: Path) -> None: ...
    def advance(self, plan_digest: str, operation: ReceiptOperation,
                stage: ReceiptStageV2, **fields) -> InterventionExecutionReceiptV2: ...
    def head(self, plan_digest: str, operation: ReceiptOperation) -> InterventionExecutionReceiptV2 | None: ...
    def verify_chain(self, plan_digest: str, operation: ReceiptOperation) -> InterventionExecutionReceiptV2: ...
```

---

## 5. 核心设计四：预算计数状态机

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
| business retest 拒绝→恢复成功 | 是 | 若 final_risk!=low | 否 | — | 否 | experiment.digest + receipt(RECOVERY).digest |
| 恢复（RECOVERY execute）失败 | 是（候选已计） | 同上 | 是（fail-closed） | 见待确认 | **是** | receipt(RECOVERY).digest |
| backend 异常（apply/verify/rollback） | 视 apply_started | 同上 | 见 §3.3 补偿结果 | — | 是（NEEDS_ATTENTION 时） | outcome.evidence_digest / receipt.digest |
| execute 意外异常但 receipt 显示 apply_started | 是 | 若 final_risk!=low | 是（fail-closed） | 见待确认 | **是**（目标未知） | receipt.digest |
| receipt 写失败（前置 / 后置） | 前置不执行；后置按 apply_started | 同上 | 前置重抛 / 后置 fail-closed | — | 是（apply 后） | receipt/attention |

- 计数基于 `InterventionOutcome`（或异常路径 receipt）的 `apply_started`，不基于
  `intervention()` 返回非 None。

---

## 6. 核心设计五：dynamic-loop schema/versioning（不变）

- `DynamicPhaseRun` 升 `v1alpha2`（新增 `risky_interventions`、`execution_receipts`），
  v1alpha1 不改（digest 用 `exclude_none=False`）；legacy 分派沿用
  `test_system_opt_optimization_run_versions.py` 模式。
- `DynamicWindowRecord` 增 `plan_digest/outcome_digest/receipt_digest`（可选）。
- `WindowAction` 增员 `GATE_REJECTED`、`INTERVENTION_FAILED`。
- `HypothesisProposal` 增 `risk/risk_kind/risk_rationale`；`HypothesisProposalsFile` 升
  `v1alpha2`，risk 无默认。缺 task risk：legacy 只读、新执行路径 fail-closed。

---

## 7. 核心设计六：phase-gate evaluator 版本化（不变）

- **v1alpha1 冻结**：`evaluate_phase_gate` + `DynamicPhaseGateContract`（含
  `risky_interventions > risk_quota`）逐字节保持。
- **v1alpha2**：`DynamicPhaseGateContractV2` + `evaluate_phase_gate_v2`（移除风险后置分支，
  配额归执行前 `evaluate_intervention_gate` 的 `>=`）。
- 分派：`gate-contract.json` 的 `schema_version` 决定 evaluator；确定性回放不变。

---

## 8. 实施拆包（三个包，链式 A → B → C）

### 8.1 D5-I2-A：L1 progress observer + backend 异常补偿 + durable receipt log

- 依赖：D5-I1。
- 写集合：`safety.py`（中立 `SafetyProgressStage/Event`、`ProgressRecordError`、
  `execute_observed()` + `ObservedSafetyResult`/`SafetyProgressFailure`、`_execute` 核心的
  backend 异常补偿）、`intervention.py`（`receipt_stage_for` 映射 + `ReceiptStageV2`/
  `ReceiptOperation`/`InterventionExecutionReceiptV2`，**v1alpha1 receipt 不动**）、新增
  `intervention_receipt.py`（`DurableReceiptStore` + `AttentionSink` 协议）、对应单测。
- 不可改：`dynamic_loop.py`、`dynamic_adapters.py`、`phase_gate.py`、`cli.py`、collection/replay、
  rollback/regression、executor backends、GPT 测试。
- API：§3.2/§3.5/§4.4；`AttentionSink`（§8.3）。
- 验收：observer 5 里程碑；前置抛 `ProgressRecordError` 且不 apply；后置累积 `progress_failures`
  且继续补偿；backend apply/verify/rollback 异常补偿 + NEEDS_ATTENTION；receipt 显式 stage 严格
  上升、前驱链/序号/分叉/倒退 fail-closed；CANDIDATE/RECOVERY 各自独立 pointer；`execute` 与
  `SafetyResult` 序列化不变。

### 8.2 D5-I2-B：dynamic adapters + dynamic loop + gate/count 接线

- 依赖：D5-I2-A。
- 写集合：`dynamic_adapters.py`（两阶段化 → `prepare_intervention`/`execute_intervention`，
  接 store + `AttentionSink`，用 `execute_observed`）、`dynamic_loop.py`（prepare→gate→execute、
  risky 计数、异常按 receipt 计数 + fail-closed）、`phase_gate.py`（`DynamicPhaseGateContractV2`
  + `evaluate_phase_gate_v2`，v1alpha1 不动）、schema/versioning v1alpha2、对应测试。
- 不可改：`safety.py`、`intervention.py`/`intervention_receipt.py`（A 完成）、`cli.py`、
  executor backends、collection/replay、rollback。
- 验收：§9 全矩阵；legacy fixture digest 不变；simulated E2E 正向不回归。

### 8.3 D5-I2-C：CLI attention sink 集成（闭合问题 4/6）

- 依赖：D5-I2-B。
- 写集合：`services/api/looper_api/cli.py`（把 `FileTargetGuard.mark_needs_attention` 注入为
  `AttentionSink`；用 `backend.capabilities.target_id` 绑定 target）、services 胶水、测试。
- 不可改：core 包 L0/L1 模块、executor backends、collection/replay、rollback、GPT 测试。
- 中立协议（定义在 A，无 target_id）：

```python
AttentionSink = Callable[[str, str], None]   # (reason, evidence_digest)；target 由 CLI 绑定
```

- 验收：receipt 后置写失败但恢复成功仍标 needs-attention；候选/恢复异常落 attention；
  `FileTargetGuard` 现有语义不变。

### 8.4 并行性

A → B → C 链式。A 内 `DurableReceiptStore`（纯）与 observer/补偿钩子可并行；B 内 `phase_gate`
v2 与 adapter/loop 弱耦合；C 只依赖接口，独立于 A/B 实现细节。

---

## 9. 测试矩阵（验收覆盖，R2 增补）

| 用例 | 预期 |
|---|---|
| low/medium/high manifest 风险 | 解析正确（接线复验） |
| task override（提高 + rationale） | 通过，final_risk 提高 |
| single-change 拒绝 | 零写、不计预算 |
| quota=K：前 K 次执行、第 K+1 次执行前停 | `evaluate_intervention_gate` 拒 |
| preflight 拒绝 | 不计数、无 apply |
| apply_started 后无 Experiment | 仍计 interventions/risky |
| backend apply 异常 | 捕获→补偿→ROLLED_BACK/NEEDS_ATTENTION，结构化记录，不冒出 |
| backend verify 异常 | 捕获→rollback |
| backend rollback 异常 | NEEDS_ATTENTION |
| verify 失败 | L1 rollback + 计数 + 无 attention |
| rollback 成功 / 失败 | 成功不 attention；失败 attention + fail-closed |
| business retest 拒绝→恢复 | CANDIDATE + 独立 RECOVERY 链，各单调、`parent_receipt_digest` 关联 |
| receipt 写失败（前置 / 后置） | 前置重抛不 apply；后置继续补偿 + attention |
| observer 抛异常（前 / 后） | 前 typed 重抛不 apply；后 `progress_failures` 不中断补偿 |
| receipt 分叉 / 倒退 / 链断开 / 悬空 pointer | fail-closed（分叉 = 同 predecessor 两个 successor） |
| CANDIDATE/RECOVERY pointer 互不覆盖 | 各自 head 正确 |
| pointer 丢失重扫 | 沿前驱链重建唯一 head |
| outcome/receipt/plan digest 不一致 | `verify_outcome_binding` / store fail-closed |
| lease 最终释放 | kept/rollback/异常/恢复后 `FileTargetGuard.release` 最终调用 |
| 原始异常上下文 | 前置 `raise ... from` 保留；后置结构化不声明链 |
| v1alpha1 确定性回放 | 旧合同+旧 state 结果不变 |
| legacy fixture digest 不变 | v1alpha1 fixture 加载 digest 口径不变 |
| `SafetyResult` 序列化不变 | `execute` 输出与 D5-I1 前逐字段一致 |
| simulated E2E 正向不回归 | `test_system_opt_dynamic_e2e.py` 全绿 |
| attention sink 注入 | 后置写失败但恢复成功仍标 needs-attention |

---

## 10. 禁止默认（只能列为任务输入或待确认）

风险阈值、wall-clock 数值、retry 次数、receipt 保留期（GC）、attention 自动清除策略、真实
Linux 写入策略——不得自选。

---

## 11. 待确认问题（R2 收窄后）

1. `tests/test_system_opt_dynamic_adapters.py` 不存在（`SafetyBackedIntervention` 由
   `dynamic_e2e.py` 测试）；D5-I2-B 的 adapter 测试放新文件还是并入 `dynamic_e2e.py`？
2. `rollback 失败` / `execute 意外异常但 apply_started` / `恢复失败` 的停止语义：复用
   `SAFETY_TRIGGERED/degradation` 还是新增 stop class / triggered_field（本文不新增停类）。
3. `write_attempted` 与 `apply_started` 当前同缝，是否接受（等未来 backend 引入独立写步骤再拆）。
4. pointer 是否保留「快路径 pointer + 链重建兜底」双轨（本文两者都保留）。
5. 缺 task risk 旧 proposal 只读 vs fail-closed（本文建议 fail-closed + 只读兼容）。
6. §3.3 的 backend 异常补偿是否对既有 `execute` 所有调用方一次性生效（本文建议是，安全修复；
   需主 agent 确认审计范围）。

---

## 12. 引用校验记录

下列符号/文件已在 `origin/system-optimizer-impl@39af89c` 确认存在：

- `dynamic_loop.py`：`run_dynamic_phase`(113)、`WindowAction`(79)、`DynamicWindowRecord`(88)、
  `DynamicPhaseRun`(97)、`interventions += 1`(270)、`risky_interventions=0`(171)。
- `hypothesis.py`：`request_intervention`(174)、`InterventionExperiment`(62)、`HypothesisRoutingError`(34)。
- `dynamic_adapters.py`：`SafetyBackedIntervention`(456)、`__call__`(492)、`DynamicInterventionError`(88)、
  `HypothesisProposal`(143)、`HypothesisProposalsFile`(154)、`BusinessRetestPlanner.judge`(433)、
  恢复 `execute(...keep=True)`(545)。
- `safety.py`：`SafetyController.execute`(109)、preflight succeeded(173)、`backend.apply`(210)、
  `backend.rollback`(379)、rollback verify(396)、round-trip(414)、KEPT(286)、NEEDS_ATTENTION(428)、
  ROLLED_BACK(428)。
- `phase_gate.py`：`evaluate_phase_gate`(134)、`risky_interventions > risk_quota`(181)、
  `DynamicPhaseGateContract`(74)、`GateDecision`(108)、`GateStopClass`(30)、`PhaseBudget`(54)。
- `executor/__init__.py`：`ExecutorBackend`(119)、`ConfigSnapshot`(74)、`OperationResult`(89)、
  `OperationStatus`(25)、`BackendCapabilities.target_id`(36)。
- `lease.py`：`FileTargetGuard`(117)、`TargetLease`(28)、`TargetAttention`(41)、`_atomic_write`(156)、
  `mark_needs_attention`(238)、`acquire`(180)、`release`(227)。
- `intervention.py`（D5-I1-R1）：`InterventionPlan/InterventionOutcome/InterventionExecutionReceipt/
  ReceiptStage/RiskSource/RiskSourceKind/ResolvedPlanRisk` + `resolve_plan_risk`、
  `evaluate_intervention_gate`、`verify_outcome_binding`、`InterventionContractError`。
- `services/api/looper_api/cli.py`：`FileTargetGuard`(70)、`mark_needs_attention`(369/634/883/1132/1530)、
  `clear_attention`(740)、`_mark_regression_attention`(360)。
- 测试：`test_system_opt_dynamic_loop.py`、`test_system_opt_dynamic_e2e.py`、
  `test_system_opt_safety.py`、`test_system_opt_intervention.py`、
  `test_system_opt_optimization_run_versions.py` 均存在。

**注意**：任务「可只读」清单中的 `tests/test_system_opt_dynamic_adapters.py` **不存在**；
`SafetyBackedIntervention` 真实测试在 `tests/test_system_opt_dynamic_e2e.py`（§11.1）。
