# D5-I2 运行时接线设计：两阶段干预 + durable receipt（设计冻结稿）

> 状态：design-only（不写生产代码，等主 agent 验收后再授权 D5-I2 实现）。
> 基线：origin/system-optimizer-impl@39af89c（D5-I1 已接入为 0c69cdd + 87d776c）。
> 日期：2026-08-24。

本文只做设计冻结：把依赖流、异常边界、写集合拆清楚。不做任何 Python/测试改动，
不自行决定风险阈值、wall-clock、retry 次数、receipt 保留期、attention 清除策略、真实
Linux 写入策略——这些都列作任务显式输入或待确认项。

---

## 1. 当前依赖流（逐节点事实）

链路（生产侧，经文件适配器）：

```
run_dynamic_phase (dynamic_loop.py)
  -> HypothesisLedger.request_intervention (hypothesis.py)
  -> intervention callback = SafetyBackedIntervention.__call__ (dynamic_adapters.py)
  -> SafetyController.execute (safety.py)
  -> backend snapshot/apply/verify/rollback (executor/__init__.py Protocol)
  -> business retest (BusinessRetestPlanner.judge + 外部 runner 补窗)
  -> InterventionExperiment (hypothesis.py)
  -> dynamic-loop interventions 计数 (dynamic_loop.py)
  -> evaluate_phase_gate (phase_gate.py)
```

### 1.1 `run_dynamic_phase`（dynamic_loop.py:113）

- 输入：`contract`（WorkloadContract）、`gate_contract`（DynamicPhaseGateContract）、
  `promotion_contract`、`environment_digest`、`max_windows`、`probe_top_k`、注入回调
  `load_identity/o0_source/hypothesis_source/clock/o1_source/component_probe/intervention/retest`、
  `verification_window_count`。
- 输出：`DynamicPhaseRun`（含 `windows`、`verification_observations`、`promotion`、
  `hypothesis_ledger_digest`、`stop_gate_decision`）。
- 是否写配置：**不直接写**；配置写入只在注入的 `intervention` 回调内发生。
- 是否抛异常：会。`intervention(head)`（line 269）抛出的任何异常**未捕获**，直接冒出
  `run_dynamic_phase`；`HypothesisRoutingError` 被捕获（line 265）转 `PROBE_BLOCKED`，
  但 `intervention` 本身异常不捕获。
- 异常时目标是否可能已改变：**可能**。`intervention` 内部可能已 `backend.apply` 后失败
  （例如 restore 失败抛 `DynamicInterventionError`）。
- 当前 durable evidence：无。只有内存中的 `DynamicPhaseRun`；`intervention-failure-*.json`
  是适配器临时产物，非版本化、非绑定 plan.digest。
- 当前计数是否准确：**不准确**。
  - `interventions += 1`（line 270）只在 `intervention(head)` **正常返回后**执行；异常时
    不递增（计数丢失）。
  - `risky_interventions` 在 `run_dynamic_phase` 内**从不更新**（`gate_state` 只更新
    `interventions`，见 line 367-381），永远为 0；因此 `evaluate_phase_gate` 的
    risk_quota 分支在真实循环里从不触发。
- D5-I2 修改层：**这里**（prepare→gate→execute 编排、risky 计数、异常路径按 receipt 计数）。

### 1.2 `HypothesisLedger.request_intervention`（hypothesis.py:174）

- 输入/输出：输入 `hypothesis_id`；无返回值或抛 `HypothesisRoutingError`（D2 规则 1：
  每症状至少两个非终态竞争假设）。
- 不写配置、不抛运行时异常（只抛业务路由错误）。
- D5-I2 修改层：不改。

### 1.3 `intervention` 回调 = `SafetyBackedIntervention.__call__`（dynamic_adapters.py:492）

- 输入：`ComponentHypothesis`；输出：`InterventionExperiment | None`。
- 写配置：**是**（经 `SafetyController.execute(..., keep=True, keep_authorized=True)`）。
- 抛异常：会。
  - `proposal is None` → 写 `control/intervention-failure-{hypothesis_id}.json` 返回 `None`；
  - apply/keep 失败 → 写 `intervention-failure-*.json` 返回 `None`；
  - **rejected 假设的恢复（rollback）失败** → 抛 `DynamicInterventionError`（line 554），
    相位经异常停下（fail-closed）。
- 异常时目标是否可能已改变：**可能**（restore 失败意味着机器仍处于已修改状态）。
- durable evidence：无版本化 receipt；`intervention-failure-*.json`（line 496/511）是临时
  记录，**不复用**（D5-I1 已锁定语义 #4）。
- 计数：回调内部不计数；计数在 loop 侧，且异常时丢失（见 1.1）。
- D5-I2 修改层：**这里**（两阶段化：prepare → gate → execute；execute 产出
  `InterventionOutcome`，异常时以 `InterventionExecutionReceipt` 落盘再 fail-closed）。

### 1.4 `SafetyController.execute`（safety.py:109）

- 输入：`manifest`、`candidate_values`、`backend`、`fencing_token`、`measure`、`keep`、
  `keep_authorized`；输出：`SafetyResult`（`state: SafetyState` + `events` + `snapshot` +
  `final_snapshot` + `applied_items` + `reason`）。
- 写配置：**是**（apply 循环 line 210 `backend.apply`；rollback 循环 line 379
  `backend.rollback`）。
- 抛异常：方法本体用 `try` 包住 preflight/snapshot 之前的部分（line 151-162）只捕获
  `KeyError`；**snapshot 之后到 apply/verify/rollback 全程不捕获 backend 异常**——backend
  抛出的异常会冒出 `execute`（此时目标可能已部分修改）。
- 异常时目标是否可能已改变：**可能**（`backend.apply` 已发生后 backend/verify 抛异常，
  或 `_rollback` 内异常）。
- durable evidence：`SafetyResult`（内存）＋可选 `Control/` 侧由调用方落盘；本层无 durable
  progress 记录。
- D5-I2 修改层：**这里**（D5-I2-A：增加可选 progress observer，见 §3）。

### 1.5 backend `snapshot/apply/verify/rollback`（executor/__init__.py:127-135）

- `ExecutorBackend` Protocol；`OperationResult`（succeeded 判定 line 100-102）、
  `ConfigSnapshot`（complete 判定 line 82-86）。
- 写配置：`apply`/`rollback` 是；`snapshot`/`verify` 只读。
- 抛异常：真实后端（local_linux/ssh_remote）可能抛；simulated 用 `SimulatedFailurePlan`
  模拟失败（不抛，返回 `FAILED` status）。
- D5-I2 修改层：不改。

### 1.6 business retest（`BusinessRetestPlanner.judge`，dynamic_adapters.py:433）

- 输入 `MeasurementBatch`，输出 `ImprovementEvidence`（含 `accepted` / `lower`）。
- 不写配置；可能抛 `RetestIdentityDrift`（身份漂移）或 `SessionFileMissing`。
- 异常时目标可能已改变：是（此时配置已 keep，等待裁决）。
- D5-I2 修改层：不改（只读消费）。

### 1.7 `InterventionExperiment`（hypothesis.py:62）

- 字段：`measurement_batch_digest`、`business_metric_id`、`accepted`、`business_lcb`。
- 纯数据，不写配置。D5-I2 不改。

### 1.8 interventions 计数（dynamic_loop.py:270）

- 见 1.1：异常路径丢失、risky 从不计数。D5-I2 必须修复（按 receipt 的 `apply_started`
  计 interventions；按 `resolved.final_risk != low` 计 risky）。

### 1.9 `evaluate_phase_gate`（phase_gate.py:134）

- 输入 `DynamicPhaseGateContract` + `PhaseGateState`；输出 `GateDecision`。
- 判定顺序固定：degradation → identity_drift → max_interventions → wall_clock →
  `risky_interventions > risk_quota`（line 181-187，**严格 `>`**）→ SLO → convergence。
- D5-I2 修改层：删除该 risk_quota 分支（§6），由 `evaluate_intervention_gate` 接管。

---

## 2. 核心设计一：prepare → gate → execute

### 2.1 接口签名（提案，供 D5-I2-B 实现）

```python
# 规划（纯，不写配置）
def prepare_intervention(
    *,
    hypothesis: ComponentHypothesis,
    proposal: HypothesisProposal,          # change 来自 proposal.change
    manifest: ConfigManifest,
    task_risk: RiskLevel,                  # 任务显式输入，缺失即 fail-closed
    risk_kind: RiskSourceKind,
    risk_rationale: str | None = None,
) -> InterventionPlan
```

```python
# 执行前门禁（D5-I1 已落地，纯）
def evaluate_intervention_gate(
    *,
    plan: InterventionPlan,
    manifest: ConfigManifest,
    contract: DynamicPhaseGateContract,
    risky_interventions: int,
    evidence_digest: str,
) -> GateDecision | None
```

```python
# 执行（真正施加/复测/回退；必须产出 Outcome，异常经 receipt 落盘）
def execute_intervention(
    *,
    plan: InterventionPlan,
    receipt: InterventionExecutionReceipt,   # 初始 stage=planned
    store: DurableReceiptStore,              # durable 落盘组件（§4）
    controller: SafetyController,
    manifest: ConfigManifest,
    backend: ExecutorBackend,
    fencing_token: int,
    planner: BusinessRetestPlanner,
) -> InterventionOutcome
```

### 2.2 关键语义

- **manifest 注入**：`manifest` 来自会话侧（`build_demo_manifest()` 或真实清单），由
  `SafetyBackedIntervention` 现已在构造时持有（dynamic_adapters.py:467-484 的
  `self._manifest`）；D5-I2-B 把该 `manifest` 传给 `prepare_intervention` 与
  `evaluate_intervention_gate`。
- **proposal.change / task risk / RiskSource 形成**：
  - `change` = `proposal.change`（参数名 → 值），与 D5-I1 的 `InterventionPlan.change`
    一致（键为 parameter_id）。
  - `task_risk` 是任务显式输入；**不得用 `risk=low` 默认值兼容旧 proposal**。
  - `RiskSource` 由 `prepare_intervention` 生成：`manifest_digest = manifest.digest`，
    `items` 按 `change` 的键逐项从 `manifest.item_for_parameter` 派生并按 `item_id`
    升序（D5-I1-R1 已强制）。
  - `kind`/`rationale` 由任务给出；`resolve_plan_risk`（在 gate 内部）校验
    manifest 下界与 kind/rationale 一致性（D5-I1-R1 已落地）。
- **prepare 阶段不写配置**：`prepare_intervention` 只构造模型，不做任何 `backend.*` 调用，
  不落盘 receipt。
- **single-change / risk-quota 必须在 execute 前完成**：`evaluate_intervention_gate` 在
  `execute_intervention` 之前调用；被拒即返回 `GateDecision`，不进 execute。
- **gate 拒绝时不计预算、不创建“执行已开始”假证据**：被拒时 receipt 保持
  `stage=planned` 或根本不创建 receipt；interventions/risky 均不递增。

---

## 3. 核心设计二：L1 进度观察缝（D5-I2-A）

### 3.1 `SafetyController.execute` 精确里程碑定位

| receipt 阶段 | SafetyController 代码位置 | 说明 |
|---|---|---|
| preflight completed | safety.py:173 `event(SafetyState.PREFLIGHT, "preflight", "succeeded")` | 所有 preflight 通过后 |
| `write_attempted` | safety.py:210 第一次 `backend.apply(item, ...)` 之前 | 当前 SafetyController 无独立“写”步骤，**write 与 apply 同一缝**；snapshot 只读不写 |
| `apply_started` | safety.py:210 第一次 `backend.apply(item, ...)` 之前 | 与 `write_attempted` 同点；若未来 backend 引入独立 prepare/commit 写步骤，二者才分离 |
| `rollback_attempted` | safety.py:379 第一次 `backend.rollback(...)` 之前（`_rollback` 内） | 进入补偿路径 |
| `rollback_verified` | safety.py:396 rollback verify 成功 且 safety.py:414 round-trip snapshot 匹配后（state 转 ROLLED_BACK） | 回退验证完成 |
| terminal safety state | `SafetyState.KEPT`(285) / `ROLLED_BACK`(441) / `NEEDS_ATTENTION`(428) / `REJECTED`(162/187) | 终态 |

### 3.2 回答（设计结论）

- **如何在第一次 `backend.apply` 前 durable 记录 `apply_started`**：在 `SafetyController.execute`
  增加**可选** `progress_observer` 钩子（协议见 3.4），在 line 210 第一次 apply 之前
  **先**同步调用 observer（把 receipt `advance(ReceiptStage.APPLY_STARTED)` 并落盘），
  落盘成功后才执行 `backend.apply`。observer 落盘失败 → 不执行 apply（拒绝执行，安全）。
- **receipt 写失败何时可安全拒绝执行**：仅当**尚未有任何 `backend.apply`** 时（
  `apply_started` 之前的 `write_attempted`/`apply_started` 里程碑）。此时目标未变，拒绝
  无副作用。
- **apply 已发生后 receipt 更新失败为何不能简单抛出并跳过 rollback**：`backend.apply`
  之后目标**可能已被修改**；此时抛出会让机器停留在已修改状态且无补偿。rollback 是独立于
  evidence 耐久性的安全义务——必须继续走 `_rollback`，把 receipt 写失败与原始执行结果
  一起归入 needs-attention / 异常链，而不是丢弃补偿。
- **是否需要给 SafetyController 加可选 progress observer**：**是**。作为可选参数（默认
  `None`），语义是「可失败、可观察的 durable 进度记录」。
- **observer 自身异常处理规则**：
  1. `apply_started` 之前的里程碑（write_attempted/apply_started）：observer 抛异常 →
     `SafetyController.execute` 立即终止，**不执行 apply**，返回 `SafetyResult(state=REJECTED,
     reason="receipt write failed before apply", ...)`，并保留原始异常链。
  2. `apply_started` 之后的里程碑（rollback_attempted/rollback_verified/terminal）：
     observer 抛异常 → **不得中断 safety 路径**；继续 rollback/verify，最终 state 仍如实
     反映（若 rollback 成功仍 ROLLED_BACK，但把 observer 失败记录到 receipt/attention 的
     `error` 字段），并把 observer 异常链到返回结果/日志。
- **如何保持现有调用方行为兼容**：`progress_observer` 默认 `None`；`None` 时行为与现状
  完全一致（无额外落盘、无新增异常路径）。现有 `SafetyController.execute(...)` 调用方
  （safety 测试、dynamic_e2e、SafetyBackedIntervention）签名不变。
- **禁止方案**：「execute 返回后再推断 apply_started」被明确禁止——异常时返回值可能不存在，
  无法据实。必须由 observer 在 apply 前推进 receipt。

### 3.3 observer 协议（提案）

```python
class ProgressObserver(Protocol):
    def __call__(self, milestone: ReceiptStage, safety_state: SafetyState) -> None: ...
```

`SafetyController.execute(..., progress_observer: ProgressObserver | None = None)`。里程碑
只前进（receipt 自身 `advance` 已保证不可倒退，intervention.py 的
`InterventionExecutionReceipt.advance`）。

---

## 4. 核心设计三：durable receipt store（D5-I2-A）

不复用 `control/intervention-failure-*.json`。新独立、版本化
`InterventionExecutionReceipt`（D5-I1 已建模，schema `looper.intervention-execution-receipt/v1alpha1`），
本设计给出落盘组件。

### 4.1 落盘布局与命名

- 内容寻址文件名：`receipts/<receipt_digest_hex>.json`，`receipt_digest_hex =
  receipt.digest.removeprefix("sha256:")`。receipt.digest 由内容决定，同一内容幂等去重。
- **plan digest 绑定**：receipt 内容含 `plan_digest`；读取时校验文件名（内容 digest）与
  内容一致，且 `receipt.plan_digest` 严格 sha256（D5-I1 已强制）。
- **current pointer**：需要。因为 receipt 内容随 stage 前进而变（digest 变），单 plan 会
  产生多条不可变记录。用单调 pointer 记录最新：`receipts/<plan_digest_hex>.current.json`
  = `{"plan_digest": ..., "latest_receipt_digest": ...}`。指针更新必须单调（新 receipt 的
  `stage` 不得早于指针所指 receipt 的 `stage`）。
- **原子写顺序**：先原子写**内容寻址 receipt 文件**（tmp + `os.replace`，复用 lease.py
  `FileTargetGuard._atomic_write` 模式，lease.py:156），成功后再原子更新 current pointer；
  指针更新失败不影响已落盘 receipt（next 启动可重扫）。

### 4.2 规则

- **状态只能前进**：`advance` 已保证（intervention.py）；store 层额外校验新 stage 的
  rank 不倒退。
- **幂等 / 重放**：写相同内容寻址文件是 no-op（内容寻址去重）；pointer 指向相同 digest
  时更新幂等；**旧 stage 重放被拒**（单调校验）。
- **fail-closed**：
  - 文件缺失 / 悬空 pointer（pointer 指向的 receipt 不存在）→ 报错（不可静默当 planned）。
  - 篡改（文件名 digest 与内容重算不符，或 plan_digest 不匹配）→ 报错。
  - stage 倒退 → 报错。
- **receipt 写失败保留原始执行异常**：observer/execute 捕获写失败时，把原始执行异常
  `raise ... from original`，receipt 的 `error` 字段记录（best-effort），不丢 traceback。
- **真实性边界（诚实声明）**：receipt 自洽（digest 可重算）≠ 真实发生；可信任锚是
  manifest digest / 外部签名，receipt 只保证「记录的进展与 plan 绑定、可回放、单调」。
  本设计**不**加签名实现。

### 4.3 API（提案）

```python
class DurableReceiptStore:
    def __init__(self, root: Path) -> None: ...
    def advance(self, receipt: InterventionExecutionReceipt,
                stage: ReceiptStage) -> InterventionExecutionReceipt: ...   # 原子落盘 + 单调 pointer
    def current(self, plan_digest: str) -> InterventionExecutionReceipt | None: ...
    def verify(self, plan_digest: str) -> InterventionExecutionReceipt: ...  # fail-closed 读取
```

---

## 5. 核心设计四：预算计数状态机

`risky` 判定统一用 `resolved.final_risk != low`（D5-I1-R1）；证据 digest 选型见每行。

| 事件 | interventions +1? | risky +1? | 停止相位? | stop class / triggered_field | attention? | 证据 digest 来源 |
|---|---|---|---|---|---|---|
| prepare 成功 | 否 | 否 | 否 | — | 否 | plan.digest（未执行） |
| gate single-change 拒绝 | 否 | 否 | 是 | BUDGET_EXHAUSTED / `single_change_per_window` | 否 | gate_state.evidence_digest（窗口） |
| gate risk-quota 拒绝 | 否 | 否 | 是 | BUDGET_EXHAUSTED / `budget.risk_quota` | 否 | gate_state.evidence_digest（窗口） |
| L1 preflight 拒绝 | 否 | 否 | 否（操作失败） | — | 否 | outcome.evidence_digest（safety result） |
| apply_started 后 kept 成功 | 是 | 若 final_risk!=low | 否 | — | 否 | outcome.evidence_digest（复测 batch） |
| apply_started 后 verify 失败→rollback | 是 | 若 final_risk!=low | 否（已补偿） | — | 否 | outcome.evidence_digest |
| rollback 失败 | 是 | 若 final_risk!=low | 是（fail-closed） | SAFETY_TRIGGERED / `degradation` 或新语义（见待确认） | **是** | receipt.digest + safety result |
| business retest 拒绝→恢复 | 是 | 若 final_risk!=low | 否（refuted，已恢复） | — | 否 | experiment.measurement_batch_digest |
| execute 意外异常但 receipt 显示 apply_started | 是 | 若 final_risk!=low | 是（fail-closed） | 见待确认 | 是（目标状态未知） | receipt.digest |
| receipt 写失败 | 视时机 | 视时机 | 见 3.2 | — | 是（若 apply 后） | receipt（best-effort） |

- **计数时机**：`interventions`/`risky_interventions` 必须基于 `execute_intervention` 产出
  的 `InterventionOutcome`（或异常路径的 receipt）的 `apply_started`，**不得**基于
  `intervention()` 是否返回非 None（D5-I1 背景问题 #3）。
- **异常路径**：`execute_intervention` 意外异常时，loop 捕获后按 receipt 的
  `apply_started` 计 interventions/risky，随后 fail-closed 停相位，并保留原始异常 +
  receipt digest。

---

## 6. 核心设计五：dynamic-loop schema/versioning

现状模型：
- `DynamicPhaseRun`（schema `looper.dynamic-phase-run/v1alpha1`，dynamic_loop.py:97-98），
  字段含 `windows`、`verification_observations`、`promotion`、`hypothesis_ledger_digest`、
  `stop_gate_decision`、`note`；`digest` 用 `exclude_none=False`（dynamic_loop.py:110）。
- `DynamicWindowRecord`（dynamic_loop.py:88）：`window_id`、`observation_window_digest`、
  `slo_met`、`action`、`hypothesis_id`、`note`。
- `WindowAction`（dynamic_loop.py:79）：`OBSERVE/SYMPTOM_REGISTERED/PROBE_BLOCKED/INTERVENED/
  VERIFIED/IDENTITY_DRIFT`。
- `HypothesisProposal`（dynamic_adapters.py:143）：`hypothesis_id/component/rank/rationale/
  change/supporting_digests`，**无 `risk`、无 `risk_source`**；`HypothesisProposalsFile`
  （schema `looper.hypothesis-proposals/v1alpha1`，dynamic_adapters.py:75）。

### 6.1 需要新增 / 不能直接改 v1alpha1

- `DynamicPhaseRun` 需新增：`risky_interventions: int`、`execution_receipts: list[str]`
  （receipt digest 列表，或单个 `stop_receipt_digest`）。**不能直接改 v1alpha1**——因为
  `digest` 用 `exclude_none=False`，加字段（即使默认 None）会改变历史 digest。
- `DynamicWindowRecord` 需新增：`plan_digest` / `outcome_digest` / `receipt_digest`
  （可选，记录该窗口的干预证据链）。
- `WindowAction` 可**安全增员**（StrEnum 增员不改既有序列化值）：建议新增
  `GATE_REJECTED`、`INTERVENTION_FAILED`（或复用 `note`）。
- `HypothesisProposal` 需新增 `risk: RiskLevel | None` + `risk_kind/risk_rationale`。

### 6.2 兼容策略（建议）

- **新 schema 版本**：`DynamicPhaseRun` 升 `v1alpha2`（新增必填 `risky_interventions` 与
  `execution_receipts`）；`HypothesisProposalsFile` 升 `v1alpha2`（每个 proposal 必填
  `risk`/`risk_kind`，risk 无默认）。
- **legacy loader 分派**：沿用 `test_system_opt_optimization_run_versions.py` 的分派加载
  模式——按 `schema_version` 分派；v1alpha1 走旧加载器，digest 口径不变；v1alpha2 走新
  加载器。
- **历史 digest 不变**：v1alpha1 加载路径不加字段、不改 digest 口径（D5-I1 §10 已声明）。
- **旧 simulated fixture 保留**：v1alpha1 fixture 继续按旧路径加载、非执行（read-only）；
  新执行路径只接受 v1alpha2。
- **缺 task risk 的策略（关键）**：**不在 v1alpha1 上默认 `risk=low`**。缺 risk 的 proposal
  → legacy 路径**只读**（不产生 InterventionPlan、不执行）；新执行路径缺 risk →
  **fail-closed**（拒绝形成 plan）。二者都不静默降级为 low。

---

## 7. 核心设计六：`evaluate_phase_gate` 旧 risk 分支

- `evaluate_intervention_gate` 负责「下一次执行前」的 `risky_interventions >= risk_quota`
  （D5-I1-R1 已用 `>=`，在 execute 前阻断第 K+1 次 risky 执行）。
- `evaluate_phase_gate` 现存 `risky_interventions > risk_quota`（phase_gate.py:181-187，
  严格 `>`）是**事后**阈值：在 `>=` 前门禁已落地后，`risky_interventions` 永远不会
  **超过** quota（K 次时第 K+1 次就被拦），故该分支在新循环里**不可达**。
- **结论**：D5-I2-B 接线验收后**删除**该分支（phase-gate R3 §8 已声明「由执行前检查接管」）。
- **避免双门禁冲突**：保留两处会双重语义（一个 `>=` 事前、一个 `>` 事后）。删除后只保留
  事前 `>=`，语义唯一。过渡期（D5-I2-B 未验收前）保留两处但循环保证 risky 永不超 quota，
  使旧分支惰性。
- **历史 GateDecision replay**：`GateDecision` 是已物化结果，删分支不影响已记录结果；但
  用新 `evaluate_phase_gate` 重放「risky>quota」的旧 state 会得到不同判定（旧停、新不停）。
  这是可接受的语义变更，需在 changelog 记录；**不需要** phase-gate 新 schema（合同字段
  `budget.risk_quota` 未变，只是「事前配额」vs「事后阈值」的语义迁移，属文档/行为层）。

---

## 8. 实施拆包（两个互不混写的包）

### D5-I2-A：L1 progress observer + durable receipt store（**必须先完成**）

- 依赖提交：D5-I1（0c69cdd + 87d776c）。
- 精确写集合：
  - `packages/core/looper_core/system_opt/safety.py`（加可选 `progress_observer` 钩子，§3）
  - 新增 `packages/core/looper_core/system_opt/intervention_receipt.py`（`DurableReceiptStore`）
  - 对应单元测试（safety observer、receipt store）
- 不可修改：`dynamic_loop.py`、`dynamic_adapters.py`、`phase_gate.py`、`cli.py`、
  `dynamic_collection/replay`、`rollback/regression`、executor backends、GPT 测试。
- API：`ProgressObserver` 协议；`SafetyController.execute(..., progress_observer=None)`；
  `DurableReceiptStore`（§4.3）。
- 验收测试：observer 在 5 个里程碑被调用；apply 前 observer 失败→不 apply、返回 REJECTED
  且保留原异常；apply 后 observer 失败→继续 rollback；receipt 单调前进；篡改/缺失/悬空/
  倒退 fail-closed；写失败保留原始异常链；`progress_observer=None` 时全量 behavior 不变。

### D5-I2-B：dynamic adapters + dynamic loop + gate/count 接线（依赖 A）

- 依赖提交：D5-I2-A。
- 精确写集合：
  - `dynamic_adapters.py`（`SafetyBackedIntervention` 两阶段化，或新增两阶段 adapter；
    产出 `InterventionOutcome`/receipt，接 `DurableReceiptStore`）
  - `dynamic_loop.py`（prepare→gate→execute 编排、risky 计数、异常路径按 receipt 计数 +
    fail-closed）
  - `phase_gate.py`（删旧 `risky_interventions > risk_quota` 分支）
  - schema/versioning（§6，新 v1alpha2 加载器）
  - 对应单元/集成测试
- 不可修改：`safety.py`（A 已完成）、executor backends、CLI、collection/replay、rollback。
- API：`prepare_intervention` / `execute_intervention`（§2.1）。
- 验收测试：§9 全矩阵；legacy fixture digest 不变；simulated E2E 正向不回归。

### 并行性

- A 与 B 顺序（B 依赖 A 的 observer + store）。
- A 内部：`DurableReceiptStore`（纯，不依赖 safety.py）可与 observer 钩子并行开发/测试。
- B 内部：`phase_gate` 删分支与 adapter/loop 接线耦合弱，可在 B 验收后独立小改动收尾。

---

## 9. 测试矩阵（验收覆盖）

| 用例 | 预期 |
|---|---|
| low/medium/high manifest 风险 | 解析正确；kind/rationale 一致性（D5-I1-R1 已测，接线后复验） |
| task override（提高 + rationale） | 通过，final_risk 提高 |
| single-change 拒绝 | 零写、不计预算、`triggered_field="single_change_per_window"` |
| quota=K：前 K 次执行、第 K+1 次执行前停 | 第 K+1 次 `evaluate_intervention_gate` 拒（`budget.risk_quota`） |
| preflight 拒绝 | 不计数、无 apply |
| apply_started 后无 Experiment | 仍计 interventions/risky（据 receipt/outcome.apply_started） |
| apply 异常 | 据 receipt 计数 + fail-closed + 保留原异常 |
| verify 失败 | rollback + 计数 + 无 attention（rollback_verified） |
| rollback 成功 / 失败 | 成功：不 attention；失败：attention + fail-closed |
| receipt 写失败 | apply 前：拒绝不写；apply 后：继续 rollback + attention |
| observer 抛异常 | apply 前中断、apply 后不中断 safety 路径（§3.2） |
| outcome/receipt/plan digest 不一致 | `verify_outcome_binding` / store fail-closed |
| lease 最终释放 | 干预结束无论 kept/rollback/异常，`FileTargetGuard.release` 最终调用（若接线引入 lease） |
| 原始异常上下文不丢 | `raise ... from original` / receipt.error 记录 |
| legacy fixture digest 不变 | v1alpha1 旧 fixture 加载后 digest 口径不变 |
| simulated E2E 正向不回归 | `test_system_opt_dynamic_e2e.py` 全绿 |

---

## 10. 禁止默认（只能列为任务输入或待确认）

以下**不得在文档中自选数值/策略**，实现时作为显式任务输入或列待确认：
风险阈值、wall-clock 数值、retry 次数、receipt 保留期（GC）、attention 自动清除策略、
真实 Linux 写入策略。

---

## 11. 待确认问题（提交主 agent 决策）

1. `tests/test_system_opt_dynamic_adapters.py` 不存在——`SafetyBackedIntervention` 现由
   `tests/test_system_opt_dynamic_e2e.py` 测试。D5-I2-B 的 adapter 测试应放新文件还是并入
   `dynamic_e2e.py`？
2. 缺 task risk 的旧 proposal：legacy 只读 vs 新执行路径 fail-closed（本文建议 fail-closed，
   只读兼容，需确认）。
3. `DynamicPhaseRun` 是否升 `v1alpha2`（本文建议是，避免 digest 漂移；需确认）。
4. `rollback 失败` / `execute 意外异常但 apply_started` 的停止语义：复用
   `SAFETY_TRIGGERED/degradation` 还是新增 stop class 或专用 triggered_field（需确认，本文
   不新增停类）。
5. `write_attempted` 与 `apply_started` 当前同缝；是否接受（本文接受，等未来 backend 引入
   独立写步骤再拆分）。
6. current pointer 方案（`<plan_hex>.current.json`）vs 纯扫描 receipts 目录（需确认）。
7. `evaluate_phase_gate` 删 risk 分支是否需要 phase-gate 新 schema（本文建议不需要，需确认）。

---

## 12. 引用校验记录（逐项用 grep 确认真实存在）

下列符号/文件已在 `origin/system-optimizer-impl@39af89c` 工作树中确认存在（本文引用其名
与关键行号）：

- `dynamic_loop.py`：`run_dynamic_phase`、`WindowAction`、`DynamicWindowRecord`、
  `DynamicPhaseRun`、`DYNAMIC_PHASE_RUN_SCHEMA`。
- `hypothesis.py`：`HypothesisLedger.request_intervention`、`InterventionExperiment`、
  `HypothesisRoutingError`、`ComponentHypothesis`。
- `dynamic_adapters.py`：`SafetyBackedIntervention`、`DynamicInterventionError`、
  `HypothesisProposal`、`HypothesisProposalsFile`、`HYPOTHESIS_PROPOSALS_SCHEMA`、
  `BusinessRetestPlanner.judge`、`RetestIdentityDrift`、`SessionFileMissing`。
- `safety.py`：`SafetyController.execute`、`SafetyState`、`_preflight`、`_rollback`、
  `SafetyResult`、`SafetyPolicy`。
- `phase_gate.py`：`evaluate_phase_gate`、`DynamicPhaseGateContract`、`GateDecision`、
  `GateStopClass`、`PhaseBudget`、`DYNAMIC_PHASE_GATE_SCHEMA`。
- `executor/__init__.py`：`ExecutorBackend`、`ConfigSnapshot`、`OperationResult`、
  `OperationStatus`、`BackendCapabilities`。
- `lease.py`：`FileTargetGuard`、`TargetLease`、`TargetAttention`、`_atomic_write`、
  `mark_needs_attention`、`acquire`、`release`。
- `intervention.py`（D5-I1-R1）：`InterventionPlan`、`InterventionOutcome`、
  `InterventionExecutionReceipt`、`ReceiptStage`、`RiskSource`、`RiskSourceKind`、
  `ResolvedPlanRisk`、`resolve_plan_risk`、`evaluate_intervention_gate`、
  `verify_outcome_binding`、`InterventionContractError`。
- 测试：`test_system_opt_dynamic_loop.py`、`test_system_opt_dynamic_e2e.py`、
  `test_system_opt_safety.py`、`test_system_opt_intervention.py`、
  `test_system_opt_optimization_run_versions.py` 均存在。

**注意**：任务「可只读」清单中的 `tests/test_system_opt_dynamic_adapters.py` **不存在**；
`SafetyBackedIntervention` 的真实测试在 `tests/test_system_opt_dynamic_e2e.py`（见 §11.1）。
