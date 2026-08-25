# D5-I2 运行时接线设计：两阶段干预 + durable receipt（R3 冻结稿）

> 状态：**R3（主 agent 复审冻结，可实施 D5-I2-A）**。R2（`4215404`）保留为修订草案。
> 基线：`system-optimizer-impl@4215404`；D5-I1 = `0c69cdd + 87d776c`。
> 日期：2026-08-24。

本文只做设计冻结（不写生产代码），不自行决定风险阈值、wall-clock、retry 次数、receipt
保留期、attention 清除策略、真实 Linux 写入策略——这些只能由后续任务显式输入。

## R3 主 agent 收口（R2 复审发现）

| # | R2 遗留 | R3 冻结结论 |
|---|---|---|
| 1 | 只覆盖 apply/verify/rollback，遗漏 preflight、snapshot、rollback readback/final snapshot 异常 | 按「首次 apply 前/后」覆盖**所有** backend 调用；apply 前失败零写 REJECTED，apply 后任一观察/补偿异常必须继续补偿或 NEEDS_ATTENTION（§3.3） |
| 2 | SafetyController 的 TERMINAL 早于业务 retest，receipt 无法再绑定 experiment/outcome | 拆为 `SAFETY_TERMINAL` 与 `OPERATION_TERMINAL`；前者由 L1 observer 写，后者由 adapter 在业务裁决及必要恢复后写（§3.1/§4.2/§4.3） |
| 3 | `DynamicPhaseRun` 冻结但仍准备就地修改嵌套 `DynamicWindowRecord/HypothesisProposal` | v1alpha1 全部嵌套类型冻结；新增 `DynamicWindowRecordV2`、`DynamicPhaseRunV2`、`HypothesisProposalV2`、`HypothesisProposalsFileV2`（§6） |
| 4 | 链规则同时出现严格上升与 `>=`，首节点、终态、parent 约束不完整 | stage 必须严格 `>` 且命中显式合法边；冻结首节点、终态、同链身份、CANDIDATE/RECOVERY parent 和单 successor 不变量（§4.2） |
| 5 | pointer 只以 `plan_digest` 定位，同一计划在另一窗口合法重试会碰撞旧链 | receipt 增加调用方显式提供的 `execution_id`，pointer 以 `(plan_digest, execution_id, operation)` 的 canonical digest 定位；当前动态循环绑定 `execution_id=window_id`（§2/§4.1） |

R3 同时拍板 R2 的六个待确认项（§11），不再把实施必需选择留给编码阶段。

## R2 继承项（已按 R3 最终语义修订）

| # | 问题 | R2 解决方案 |
|---|---|---|
| 1 | CANDIDATE/RECOVERY 共用一个 `current.json` 互相覆盖 | pointer 按 `(plan_digest, execution_id, operation)` 隔离；文件名使用 plan+execution 的 canonical digest，既不互相覆盖，也允许同 plan 在不同窗口重试（§4.1） |
| 2 | 同阶段分叉规则与 PREFLIGHT/TERMINAL 字段更新矛盾 | receipt 升 v1alpha2 用显式 stage，拆出 `SAFETY_TERMINAL`/`OPERATION_TERMINAL`；每次推进严格上升，分叉检测为「每个 predecessor 至多一个 successor」（§4.2） |
| 3 | backend 异常后不捕获、不补偿 | D5-I2-A 在 `_execute` 对全部 backend 调用按 apply 前/后捕获，路由 REJECTED、补偿或 NEEDS_ATTENTION（§3.3） |
| 4 | AttentionSink 缺 target_id 来源 | 协议改为**无 target_id**：`Callable[[reason, evidence_digest], None]`；D5-I2-C 用 `backend.capabilities.target_id` 绑定（§8.3） |
| 5 | ProgressRecordError 转返回值后异常链丢失 | apply 前重新抛出并保留最后一条已成功 receipt；apply 后走结构化结果、取消异常链声明（§3.4） |
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
    *, plan: InterventionPlan, execution_id: str,
    store: DurableReceiptStore,
    controller: SafetyController,
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
- `execute_intervention` 不接受调用方构造的 receipt；它必须先调用
  `store.start(plan=plan, execution_id=execution_id, operation=CANDIDATE)` 原子创建唯一 `PLANNED`
  首节点，成功后才可进入 L1。`execution_id` 是编排器显式输入；当前 dynamic loop 使用同一 session
  内唯一的 `window_id`，不得随机生成或默认补齐。已有同 execution candidate 链时 fail-closed，
  但同一个 plan 在不同窗口可形成相互独立的执行链。
- `target_id` 不在 `execute_intervention` 签名里；需要时（attention/lease）从
  `backend.capabilities.target_id`（executor/__init__.py:36）取，见 §8.3。

---

## 3. 核心设计二：L1 进度观察缝（D5-I2-A）——R3 最终语义

### 3.1 L1 里程碑定位（R3 区分业务终态）

| SafetyProgressStage | SafetyController 位置 |
|---|---|
| PREFLIGHT_COMPLETED | safety.py:173 |
| APPLY_STARTED | safety.py:210 第一次 `backend.apply` 之前 |
| ROLLBACK_STARTED | safety.py:379 第一次 `backend.rollback` 之前 |
| ROLLBACK_VERIFIED | safety.py:396 + safety.py:414 |
| SAFETY_TERMINAL | KEPT@286 / ROLLED_BACK@428 / NEEDS_ATTENTION@428 / REJECTED@162/187 的 `SafetyResult` 构造完成后；只表示一次 L1 execute 结束，不表示业务实验结束 |

### 3.2 中立类型（消除循环依赖，不变）

```python
# safety.py（不 import intervention.py）
class SafetyProgressStage(StrEnum):
    PREFLIGHT_COMPLETED = "preflight-completed"
    APPLY_STARTED = "apply-started"
    ROLLBACK_STARTED = "rollback-started"
    ROLLBACK_VERIFIED = "rollback-verified"
    SAFETY_TERMINAL = "safety-terminal"

class SafetyProgressEvent(StrictModel):
    stage: SafetyProgressStage
    safety_state: SafetyState
    item_id: str | None = None
    operation: str | None = None
    evidence_digest: str | None = None       # 仅 SAFETY_TERMINAL 必填，绑定 SafetyResult.digest
```

`intervention.py` 提供映射 `receipt_stage_for(stage: SafetyProgressStage) -> ReceiptStageV2`
（五档全映射，无 None，见 §4.2）。`OPERATION_TERMINAL` 不来自 SafetyController；它只能由
adapter 在业务 retest 和必要的 RECOVERY 完成后推进。依赖方向
`safety ← intervention ← intervention_receipt` 无环。

`SafetyResult` 不加序列化字段，只增加计算属性
`digest = canonical_digest(model_dump(exclude_none=False))`。`SAFETY_TERMINAL` 必须在 result 完整构造
后发出并携带该 digest；其它 progress stage 禁止伪造终态 evidence digest。

### 3.3 backend 全异常边界（问题 3，R3 完整覆盖）

D5-I2-A 在 `SafetyController` 的**私有 `_execute` 核心**（供 `execute` 与 `execute_observed`
共用）对每个 backend 调用建立边界。判定依据只有一个：第一次 `backend.apply` 是否可能开始；
不能按异常类型猜测目标是否改变。

**首次 apply 前（目标未写）**：

- `backend.capabilities` / `preflight_check(...)` 抛异常 → 结构化 PREFLIGHT failed 事件，
  `SafetyState.REJECTED`，不调用 apply。
- 基线 `backend.snapshot(...)` 抛异常或返回不完整 → 结构化 SNAPSHOT failed，
  `SafetyState.REJECTED`，不调用 apply。

**首次 apply 已准备开始（目标可能已写）**：

- 在调用 `backend.apply(...)` 前先把当前 item 加入 `applied`，并成功持久化
  `APPLY_STARTED`；apply 抛异常 → 结构化 APPLY failed，立即进入 `_rollback`。
- candidate `backend.verify(...)` 抛异常 → 结构化 VERIFY failed，进入 `_rollback`。
- keep 路径的最终 `backend.snapshot(...)` 抛异常或不完整 → 不得直接冒出；进入 `_rollback`。

**补偿内部（已经承担恢复义务）**：

- `backend.rollback(...)` 抛异常：记录该 item rollback failed，继续处理其余 applied items。
- rollback readback `backend.verify(...)` 抛异常：记录该 item verify failed，继续处理其余项。
- rollback 最终 round-trip `backend.snapshot(...)` 抛异常或不完整：`final_snapshot=None`（或保留
  不完整快照）、`rollback_failed=True`，终态必须为 `SafetyState.NEEDS_ATTENTION`。
- 只有所有 rollback/readback 成功且完整 final snapshot digest 等于 baseline，才可返回
  `ROLLED_BACK`。

异常统一写入既有 `SafetyEvent.operation/status/reason`：`reason` 使用有界的
`<ExceptionType>: <message>` 文本，不给 `SafetyEvent` 增字段，也不把 Python 异常对象放进模型。
这个行为对 `execute` 和 `execute_observed` 一次性生效，是主 agent 已确认的
安全修复；必须对 `SafetyBackedIntervention`、`engine/loop.py`、`rollback/regression.py` 和全部
safety tests 做回归。`SafetyResult` 字段仍冻结（§3.5）。

### 3.4 observer 异常契约（问题 5）——按 apply seam 划界

- `PLANNED` receipt 由 adapter 在调用 L1 前持久化；该写失败时根本不调用 L1。
- **首次 backend apply 尚未开始**：PREFLIGHT_COMPLETED、APPLY_STARTED 或无写终态的 observer
  持久化失败，都以 `raise ProgressRecordError(...) from observer_error` 重新抛出，不转普通
  Outcome，不执行 apply，也不标 target attention。若同一 receipt 通道不可写，只能保留已经
  成功发布的最后一个 receipt 和 Python 异常链；不得宣称失败 receipt 必然已持久化。
- **APPLY_STARTED 已成功持久化、backend apply 即将或已经开始后**：后续
  ROLLBACK_STARTED/ROLLBACK_VERIFIED/SAFETY_TERMINAL observer 失败不得中断补偿；由
  `execute_observed` 累积为 `ObservedSafetyResult.progress_failures`。调用方必须 fail-closed，
  并经独立 AttentionSink 标记目标。首次后置持久化失败后 receipt observer 进入 tainted 状态，
  后续里程碑只记录结构化 failure、不得越过缺口继续推进 receipt；adapter 也不得再写
  `OPERATION_TERMINAL`。这里明确使用结构化错误，不承诺 Python 异常链。

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

    @property
    def digest(self) -> str: ...             # canonical_digest(exclude_none=False)
```

- 只有 `execute_observed` 走 observer/补偿信封；`execute`（无 observer）路径与现状一致，
  唯一差异是 §3.3 的 backend 异常补偿（安全修复，对两者都生效）。

---

## 4. 核心设计三：durable receipt store（D5-I2-A）——R3 最终语义

### 4.1 落盘布局与 pointer 命名（问题 1）

- 内容寻址：`receipts/<receipt_digest_hex>.json`。
- **pointer 按 execution + operation 分离**。先计算
  `execution_digest = canonical_digest({"plan_digest": plan.digest, "execution_id": execution_id})`，再写：
  - `receipts/<execution_digest_hex>.candidate.current.json`
  - `receipts/<execution_digest_hex>.recovery.current.json`
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
    SAFETY_TERMINAL = "safety-terminal"          # one L1 execute ended
    OPERATION_TERMINAL = "operation-terminal"    # business/recovery operation ended

class ReceiptOperation(StrEnum):
    CANDIDATE = "candidate"
    RECOVERY = "recovery"

class InterventionExecutionReceiptV2(StrictModel):
    schema_version: Literal["looper.intervention-execution-receipt/v1alpha2"]
    plan_digest: str = Field(pattern=_DIGEST)
    execution_id: str = Field(min_length=1, max_length=160)
    operation: ReceiptOperation
    stage: ReceiptStageV2                     # 显式，非从 flag 派生
    sequence: int = Field(ge=0)
    predecessor_receipt_digest: str | None = Field(default=None, pattern=_DIGEST)
    parent_receipt_digest: str | None = Field(default=None, pattern=_DIGEST)  # RECOVERY 关联 CANDIDATE safety terminal
    plan: InterventionPlan | None = None       # CANDIDATE/PLANNED 首节点必须携带
    safety_state: SafetyState | None = None
    evidence_digest: str | None = Field(default=None, pattern=_DIGEST)
    outcome: InterventionOutcome | None = None # 仅 CANDIDATE/OPERATION_TERMINAL 携带
    error: str | None = Field(default=None, min_length=1, max_length=2000)

    @property
    def digest(self) -> str: ...             # canonical_digest(model_dump(exclude_none=False))

    @property
    def execution_digest(self) -> str: ...   # canonical digest of plan_digest + execution_id
```

- 映射 `receipt_stage_for`：`PREFLIGHT_COMPLETED→PREFLIGHT_COMPLETED`、`APPLY_STARTED→
  APPLY_STARTED`、`ROLLBACK_STARTED→ROLLBACK_ATTEMPTED`、`ROLLBACK_VERIFIED→ROLLBACK_VERIFIED`、
  `SAFETY_TERMINAL→SAFETY_TERMINAL`。`OPERATION_TERMINAL` 只由 adapter 推进。每个持久化
  里程碑对应一个更高 stage，推进必须严格上升。
- 首节点固定：`sequence=0`、`stage=PLANNED`、`predecessor_receipt_digest=None`。
  CANDIDATE 首节点必须内嵌 `plan` 且 `plan.digest == plan_digest`，parent 必须为空；RECOVERY
  首节点不重复内嵌 plan，parent 必须指向同 plan 的 CANDIDATE `SAFETY_TERMINAL` receipt。
- successor 固定：`sequence == predecessor.sequence + 1`、predecessor digest 精确匹配，且
  `successor.stage rank > predecessor.stage rank`；同时必须命中以下显式边之一，不能只凭 rank
  跳过安全里程碑：

```text
PLANNED -> PREFLIGHT_COMPLETED | SAFETY_TERMINAL
PREFLIGHT_COMPLETED -> APPLY_STARTED | SAFETY_TERMINAL
APPLY_STARTED -> ROLLBACK_ATTEMPTED | SAFETY_TERMINAL
ROLLBACK_ATTEMPTED -> ROLLBACK_VERIFIED | SAFETY_TERMINAL
ROLLBACK_VERIFIED -> SAFETY_TERMINAL
SAFETY_TERMINAL -> OPERATION_TERMINAL
```

  `PLANNED -> SAFETY_TERMINAL` 只表达 preflight 完成前的零写拒绝；
  `PREFLIGHT_COMPLETED -> SAFETY_TERMINAL` 还允许 snapshot 拒绝或“所有请求值均未改变”的零写
  KEPT/ROLLED_BACK（以及外部漂移导致的 NEEDS_ATTENTION），但一律不得据此计预算。其它提前 safety
  terminal 必须与相应 `safety_state` 一致。plan_digest、execution_id、operation、recovery parent 在
  整条链内不得漂移。
- **分叉检测 = 每个 predecessor 至多一个 successor**；同一 predecessor 出现两个不同 digest
  立即 fail-closed。`OPERATION_TERMINAL` 不得有 successor。
- `outcome` 只允许出现在 CANDIDATE `OPERATION_TERMINAL`，且必须存在并通过
  `verify_outcome_binding(plan, outcome)`；其它 stage/RECOVERY 禁止携带 outcome。RECOVERY
  `OPERATION_TERMINAL` 必须以 `evidence_digest` 绑定其完整 `SafetyResult`。
- `outcome.evidence_digest` 绑定**决定操作终态的直接证据**：L1 未进入业务复测时绑定 candidate
  `SafetyResult.digest`；业务接受时绑定 `experiment.measurement_batch_digest`；业务拒绝并进入
  RECOVERY 时绑定 recovery `SafetyResult.digest`。每个 `SAFETY_TERMINAL.evidence_digest` 必须等于
  对应 L1 `SafetyResult.digest`；若存在 progress failure，则用 `ObservedSafetyResult.digest` 作为
  attention 证据且禁止写 operation terminal。`outcome` 自身内嵌 experiment，candidate
  operation-terminal receipt 又内嵌 outcome，因此不靠一个 digest 冒充完整证据图，也不产生 receipt
  自引用。`write_attempted/apply_started` 只描述 candidate 写入，RECOVERY 不重复增加预算计数。
- **RECOVERY 作用域（问题 4 背景）**：候选 L1 先到 CANDIDATE `SAFETY_TERMINAL`；业务拒绝后，
  第二次 `execute(keep=True)` 产生独立 RECOVERY 链，其 parent 指向该 candidate safety terminal。
  RECOVERY 到 `OPERATION_TERMINAL` 后，adapter 才把最终 `InterventionOutcome` 写入 CANDIDATE
  `OPERATION_TERMINAL`。业务接受时无需 RECOVERY，candidate safety terminal 可直接推进 operation
  terminal。两条链各自单调，不存在 apply_started 倒退，也不会在业务裁决前伪称操作终态。
- pointer 丢失重扫：沿 `predecessor_receipt_digest` 回溯重建 head（= 未被引为 predecessor
  的那条）；多头 / 链断开 → fail-closed。

### 4.3 失败语义

- 缺失 / 悬空 pointer / 篡改 / 倒退 / 分叉 / 链断开 → fail-closed。
- receipt 写失败保留原始异常：前置重新抛出（`raise ... from`）；后置走结构化累积，不声明
  异常链（§3.4）。
- durable receipt 证明“执行到了哪一安全边界”，**不是独立的自动 crash rollback 日志**：它不
  复制 baseline snapshot，也不得在进程重启后盲目重放 apply/rollback。CLI 启动时发现
  `APPLY_STARTED` 及之后但尚未 `OPERATION_TERMINAL` 的 head，必须阻止新动态执行、把 receipt
  digest 交给既有 lease/state reconcile 路径并标 needs-attention；只有对账恢复完成后才可由显式
  operator 流程继续。这个启动扫描在 D5-I2-C 落地。
- 真实性边界：自洽 ≠ 真实；可信任锚是 manifest digest / 外部签名；不加签名实现。

### 4.4 API

```python
class DurableReceiptStore:
    def __init__(self, root: Path) -> None: ...
    def start(self, *, plan: InterventionPlan, execution_id: str,
              operation: ReceiptOperation,
              parent_receipt_digest: str | None = None) -> InterventionExecutionReceiptV2: ...
    def advance(self, current: InterventionExecutionReceiptV2,
                stage: ReceiptStageV2, **fields) -> InterventionExecutionReceiptV2: ...
    def head(self, plan_digest: str, execution_id: str,
             operation: ReceiptOperation) -> InterventionExecutionReceiptV2 | None: ...
    def verify_chain(self, plan_digest: str, execution_id: str,
                     operation: ReceiptOperation) -> InterventionExecutionReceiptV2: ...
```

`start`/`advance` 都先验证完整链和当前 pointer，再写新内容；目标内容文件已存在时必须重读并
核对 digest/字节语义后才视为幂等，不能仅凭 `Path.exists()` 跳过。pointer 文件使用版本化严格
模型，只允许纯文件名和 canonical lowercase sha256；扫描重建仅消费匹配
plan+execution+operation 的
合法前缀文件，同范围断链、孤儿或多 head 一律拒绝。内容文件已发布而 pointer 仍指向该唯一链
的合法祖先时，视为“内容成功、pointer 替换前崩溃”的可恢复缝：唯一内容链头优先，并在下一次
写入时重发 pointer；pointer 指向链外、身份不符或内容不存在仍 fail-closed。

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
| L1 rollback 失败 | 是 | 若 final_risk!=low | 是（fail-closed） | SAFETY_TRIGGERED / `intervention.rollback` | **是** | receipt(CANDIDATE).digest |
| business retest 拒绝→恢复成功 | 是 | 若 final_risk!=low | 否 | — | 否 | outcome.digest + candidate/recovery terminal receipt digests |
| 恢复（RECOVERY execute）失败 | 是（候选已计） | 同上 | 是（fail-closed） | SAFETY_TRIGGERED / `intervention.recovery` | **是** | receipt(RECOVERY).digest |
| backend 异常（apply/verify/rollback） | 视 candidate apply_started | 同上 | 见 §3.3 补偿结果 | SAFETY_TRIGGERED / `intervention.execution`（停止时） | 是（NEEDS_ATTENTION 时） | outcome.evidence_digest / receipt.digest |
| execute 意外异常但 receipt 显示 apply_started | 是 | 若 final_risk!=low | 是（fail-closed） | SAFETY_TRIGGERED / `intervention.execution` | **是** | receipt.digest |
| receipt 写失败（前置 / 后置） | 前置不执行；后置按 candidate apply_started | 同上 | 前置重抛 / 后置 fail-closed | SAFETY_TRIGGERED / `intervention.receipt`（后置） | 是（apply 后） | 最后一条有效 receipt / attention |

- 计数基于 `InterventionOutcome`（或异常路径 receipt）的 `apply_started`，不基于
  `intervention()` 返回非 None。

---

## 6. 核心设计五：dynamic-loop schema/versioning（R3 嵌套类型冻结）

- 现有 `DynamicWindowRecord`、`DynamicPhaseRun`、`HypothesisProposal`、
  `HypothesisProposalsFile` 及其 v1alpha1 schema/digest 逐字段冻结，禁止给旧类增加哪怕可选字段。
- 新增 `DynamicWindowRecordV2`：在复制 v1 字段的基础上增加必需的 `plan_digest`、
  `outcome_digest`、`candidate_receipt_digest`，以及可选 `recovery_receipt_digest`。它的既有
  `window_id` 同时就是 receipt `execution_id`；只有没有发生干预的窗口允许这些字段全部为空，
  字段组合由模型校验，不能产生悬空半链。
- 新增 `DynamicPhaseRunV2`（`looper.dynamic-phase-run/v1alpha2`）：窗口类型固定为
  `DynamicWindowRecordV2`，新增 `risky_interventions` 和去重、确定顺序的
  `execution_receipts`；新执行路径只产 v2。
- 新增 `HypothesisProposalV2`：risk、risk_kind 必填，risk_rationale 按 kind 规则校验；新增
  `HypothesisProposalsFileV2`（`looper.hypothesis-proposals/v1alpha2`）并只包含 V2 proposal。
  v1 proposal 文件仅允许 legacy read-only，不形成 plan；新执行路径遇到 v1 或缺 task risk
  一律 fail-closed，不默认 low。
- `WindowAction` 可增加既有序列化值之外的新成员 `GATE_REJECTED`、`INTERVENTION_FAILED`；旧成员
  值不变。
- loader 必须先读 `schema_version` 再分派到不同模型类，沿用
  `test_system_opt_optimization_run_versions.py` 的 legacy fixture/digest 保护方式；禁止先用 V2
  模型解析 v1 再回填默认值。

---

## 7. 核心设计六：phase-gate evaluator 版本化（不变）

- **v1alpha1 冻结**：`evaluate_phase_gate` + `DynamicPhaseGateContract`（含
  `risky_interventions > risk_quota`）逐字节保持。
- **v1alpha2**：`DynamicPhaseGateContractV2` + `evaluate_phase_gate_v2`（移除风险后置分支，
  配额归执行前 `evaluate_intervention_gate` 的 `>=`）。
- 分派：`gate-contract.json` 的 `schema_version` 决定 evaluator；确定性回放不变。
- `DynamicPhaseGateContractV2` 复用相同的 budget/degradation 子合同，但必须是独立版本类。
  D5-I2-A 先在 `intervention.py` 定义只暴露执行前门禁所需字段的
  `InterventionGateContract` Protocol，并让 `evaluate_intervention_gate` 依赖该协议；v1/v2 都显式
  满足协议，不依赖未声明的 duck typing。新执行路径只接受 v2；v1 仅供旧回放。

---

## 8. 实施拆包（三个包，链式 A → B → C）

### 8.1 D5-I2-A：L1 progress observer + backend 异常补偿 + durable receipt log

- 依赖：D5-I1。
- 写集合：`safety.py`（中立 `SafetyProgressStage/Event`、`ProgressRecordError`、
  `execute_observed()` + `ObservedSafetyResult`/`SafetyProgressFailure`、`_execute` 核心的
  backend 全异常补偿）、`intervention.py`（`InterventionGateContract` Protocol、
  `receipt_stage_for` 映射 + `ReceiptStageV2`/`ReceiptOperation`/
  `InterventionExecutionReceiptV2`，**v1alpha1 receipt 不动**）、新增
  `intervention_receipt.py`（`DurableReceiptStore` + `AttentionSink` 协议）、对应单测。
- 不可改：`dynamic_loop.py`、`dynamic_adapters.py`、`phase_gate.py`、`cli.py`、collection/replay、
  rollback/regression、executor backends、GPT 测试。
- API：§3.2/§3.5/§4.4；`AttentionSink`（§8.3）。
- 验收：observer 5 个 L1 里程碑；apply 前持久化失败抛 `ProgressRecordError` 且零写；apply 后
  失败累积 `progress_failures` 且继续补偿；preflight/baseline snapshot/apply/verify/keep snapshot/
  rollback/rollback verify/final snapshot 的异常矩阵全部钉死；receipt 显式 stage 严格上升，
  首节点/前驱/合法边/单 successor/终态/parent/身份漂移全部 fail-closed；CANDIDATE/RECOVERY pointer
  独立；`execute` 与 `SafetyResult` 序列化不变。

### 8.2 D5-I2-B：dynamic adapters + dynamic loop + gate/count 接线

- 依赖：D5-I2-A。
- 写集合：`dynamic_adapters.py`（两阶段化 → `prepare_intervention`/`execute_intervention`，
  接 store + `AttentionSink`，用 `execute_observed`）、`dynamic_loop.py`（prepare→gate→execute、
  risky 计数、异常按 receipt 计数 + fail-closed）、`phase_gate.py`（`DynamicPhaseGateContractV2`
  + `evaluate_phase_gate_v2`，v1alpha1 不动）、四个独立 V2 嵌套模型与 loader 分派（§6）、
  candidate `SAFETY_TERMINAL → OPERATION_TERMINAL` 及独立 recovery 链、对应测试。
- 不可改：`safety.py`、`intervention.py`/`intervention_receipt.py`（A 完成）、`cli.py`、
  executor backends、collection/replay、rollback。
- 验收：§9 全矩阵；legacy fixture digest 不变；simulated E2E 正向不回归。

### 8.3 D5-I2-C：CLI attention sink 集成（闭合问题 4/6）

- 依赖：D5-I2-B。
- 写集合：`services/api/looper_api/cli.py`（把 `FileTargetGuard.mark_needs_attention` 注入为
  `AttentionSink`；用 `backend.capabilities.target_id` 绑定 target；启动前扫描 receipt 非终态
  head 并接入既有 lease/state reconcile 边界）、services 胶水、测试。
- 不可改：core 包 L0/L1 模块、executor backends、collection/replay、rollback、GPT 测试。
- 中立协议（定义在 A，无 target_id）：

```python
AttentionSink = Callable[[str, str], None]   # (reason, evidence_digest)；target 由 CLI 绑定
```

- 验收：receipt 后置写失败但恢复成功仍标 needs-attention；候选/恢复异常落 attention；重启遇到
  apply_started 后非终态 receipt 时禁止再次执行并标 attention，不盲目自动恢复；
  `FileTargetGuard` 现有语义不变。

### 8.4 并行性

A → B → C 链式。A 内 `DurableReceiptStore`（纯）与 observer/补偿钩子可并行；B 内 `phase_gate`
v2 与 adapter/loop 弱耦合；C 只依赖接口，独立于 A/B 实现细节。

---

## 9. 测试矩阵（验收覆盖，R3 冻结）

| 用例 | 预期 |
|---|---|
| low/medium/high manifest 风险 | 解析正确（接线复验） |
| task override（提高 + rationale） | 通过，final_risk 提高 |
| single-change 拒绝 | 零写、不计预算 |
| quota=K：前 K 次执行、第 K+1 次执行前停 | `evaluate_intervention_gate` 拒 |
| preflight 拒绝 | 不计数、无 apply |
| preflight/baseline snapshot 异常 | 结构化 REJECTED、零写、receipt 到 SAFETY_TERMINAL |
| apply_started 后无 Experiment | 仍计 interventions/risky |
| backend apply 异常 | 捕获→补偿→ROLLED_BACK/NEEDS_ATTENTION，结构化记录，不冒出 |
| backend verify 异常 | 捕获→rollback |
| keep final snapshot 异常 | 捕获→rollback，不得在已 apply 后冒出 |
| backend rollback / rollback verify 异常 | 继续其余项，最终 NEEDS_ATTENTION |
| rollback final snapshot 异常/不完整 | NEEDS_ATTENTION，不能宣称 round-trip verified |
| verify 失败 | L1 rollback + 计数 + 无 attention |
| rollback 成功 / 失败 | 成功不 attention；失败 attention + fail-closed |
| business retest 接受 | candidate SAFETY_TERMINAL 后写 OPERATION_TERMINAL，内嵌绑定 outcome |
| business retest 拒绝→恢复 | RECOVERY parent 指 candidate SAFETY_TERMINAL；恢复结束后才写 candidate OPERATION_TERMINAL |
| safety terminal 证据 | terminal event/receipt digest 精确绑定完整 SafetyResult；非 terminal event 不得携带 |
| outcome 终态证据选择 | 未复测/接受/拒绝恢复分别绑定 candidate SafetyResult / measurement batch / recovery SafetyResult digest，无 receipt 自引用 |
| receipt 写失败（前置 / 后置） | 前置重抛不 apply；后置继续补偿 + attention |
| observer 抛异常（前 / 后） | 前 typed 重抛不 apply；后 `progress_failures` 不中断补偿 |
| receipt 分叉 / 倒退 / 链断开 / 悬空 pointer | fail-closed（分叉 = 同 predecessor 两个 successor） |
| CANDIDATE/RECOVERY pointer 互不覆盖 | 各自 head 正确 |
| 同 plan 不同 execution_id | 形成两组独立 candidate/recovery pointer；同 execution 重复 start fail-closed |
| 非法首节点/跳阶段/同 predecessor 分叉/terminal successor/plan-execution-operation-parent 漂移 | 全部 fail-closed |
| pointer 丢失重扫 | 沿前驱链重建唯一 head |
| outcome/receipt/plan digest 不一致 | `verify_outcome_binding` / store fail-closed |
| lease 最终释放 | kept/rollback/异常/恢复后 `FileTargetGuard.release` 最终调用 |
| 原始异常上下文 | 前置 `raise ... from` 保留；后置结构化不声明链 |
| v1alpha1 确定性回放 | 旧合同+旧 state 结果不变 |
| legacy fixture digest 不变 | v1alpha1 fixture 加载 digest 口径不变 |
| v1 嵌套模型冻结 | Window/Run/Proposal/ProposalFile 的历史 JSON 与 digest 不变 |
| `SafetyResult` 序列化不变 | `execute` 输出与 D5-I1 前逐字段一致 |
| simulated E2E 正向不回归 | `test_system_opt_dynamic_e2e.py` 全绿 |
| attention sink 注入 | 后置写失败但恢复成功仍标 needs-attention |
| 重启发现 apply 后非终态 receipt | 阻止新执行，绑定 receipt digest 标 attention，转既有 reconcile；不自动重放 |

---

## 10. 禁止默认（只能列为任务显式输入）

风险阈值、wall-clock 数值、retry 次数、receipt 保留期（GC）、attention 自动清除策略、真实
Linux 写入策略——不得自选。

---

## 11. R3 实施决策（主 agent 已确认）

1. 新建 `tests/test_system_opt_dynamic_adapters.py` 承载 adapter 两阶段和 receipt 专属测试；
   `test_system_opt_dynamic_e2e.py` 只保留跨模块正向/故障 E2E，不继续堆单元矩阵。
2. rollback 失败、apply_started 后未知异常、恢复失败都复用 `GateStopClass.SAFETY_TRIGGERED`，
   但不得伪装成 degradation：分别使用 `intervention.rollback`、`intervention.execution`、
   `intervention.recovery`、`intervention.receipt` 作为专用 triggered_field。暂不新增 stop enum。
3. 当前 backend 没有独立 prepare-write seam，接受 `write_attempted == apply_started`；未来只有在
   backend 合同版本升级并提供独立写步骤时才拆分，当前不得推测两个不同时间点。
4. 保留 pointer 快路径 + 严格前驱链重建兜底；pointer 缺失可重建，pointer 悬空/篡改、链断开、
   多 head 或同 predecessor 分叉均 fail-closed。
5. v1 hypothesis proposal 只读兼容；v2 新执行路径缺 task risk 或收到 v1 proposal 时 fail-closed。
6. §3.3 backend 全异常边界对既有 `execute` 和新 `execute_observed` 一次性生效；这是安全修复，
   必须以全部既有调用方回归作为合入门，不保留旧的“apply 后异常直接冒出”行为。
7. receipt 执行实例身份由调用方显式提供；当前 dynamic loop 以 session 内唯一 `window_id` 作为
   `execution_id`，pointer 使用 plan+execution digest，不允许只按 plan 覆盖旧执行。
8. durable receipt 不替代既有崩溃对账；非终态 receipt 在重启时只触发阻断、attention 和
   lease/state reconcile，不自动重放配置写入。

以上决定只冻结控制流和证据语义，不提供任何风险阈值、重试次数、GC、attention 自动清除或
真实 Linux 写入默认值。

---

## 12. 引用校验记录

下列符号/文件最初在 `origin/system-optimizer-impl@39af89c` 核实，并由主 agent 在
`system-optimizer-impl@4215404` 复查；其间主线提交未改变这些接口：

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
