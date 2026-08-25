# DYN-END-01D：动态相位显式窗口终点设计（docs-only）

> 状态：设计稿，**等待用户确认方案后才允许实现（DYN-END-01I）**
> 任务来源：交接执行计划（本地未跟踪）§3 DYN-END-01D；
> 缺口登记：`unfinished-task-queue-2026-08-24.md` L44/L132（DYN-END-01）；
> 发现运行：REAL-M3-01（2026-08-25，`8.134.104.213`，证据包
> `.artifacts/real-demo-2026-08-25/`，sha256 `0cd520cd…3fcb28c`）
> 基线：`system-optimizer-impl@8439654`
> 本文档为唯一写集合产物：不改任何生产代码 / 测试 / 阈值。

---

## 1. 缺口与口径

**口径**（`architecture/overall.md` §3.3 / L114）：动态优化必须有显式终点，
停止原因可回放。`workload-tuning.md` D4 把结束门禁定义为第一等约束。

**缺口**：`dynamic-run` 的 `--max-windows` 是**循环参数**而非 gate budget。
窗口耗尽时运行正常返回，但 `stop_gate_decision.stop` 仍可能为 `false`
（reason 为 "no stop class triggered; the dynamic phase continues"），仅靠
run 级 `note="window budget reached; the final gate decision is recorded as-is"`
说明结束原因。note 不是版本化停止证据，无法作为可回放终点。

**真实工件证实**（已逐字段核对，非推测）：

- `.artifacts/real-demo-2026-08-25/downloaded-evidence/run-20260825/dynamic-run.json`
  与 `run-20260825-always/dynamic-run.json` 两个相位均为：
  `schema_version=looper.dynamic-phase-run/v1alpha2`，6 个窗口耗尽，
  `stop=false`、`stop_class=null`、`triggered_field=null`，note 为
  "window budget reached…"。两相位 gate 合同均为 v1alpha2。

## 2. 已核实的代码事实（本设计的地基）

以下全部经源码阅读 + 真实工件复算验证（见 §2.7 验证记录），标注文件与行号
以基线 `8439654` 为准。

### 2.1 gate 合同侧（`packages/core/looper_core/system_opt/phase_gate.py`）

| 事实 | 位置 | 设计含义 |
|---|---|---|
| 两个 gate schema：`v1alpha1` / `v1alpha2`，`load_dynamic_phase_gate` 按 `schema_version` 分发、不做迁移 | L28-29, L115-125 | 新方案只能**新增版本分支**，不能改 v1/v2 模型 |
| `PhaseBudget = {max_interventions, wall_clock_seconds, risk_quota}`，被 v1/v2 两个合同类**共享** | L56-61, L82, L104 | 给共享类加必填字段会炸旧 loader；加带默认字段会让旧 payload 重算 digest 漂移。**两条路都不可行**，新窗口预算必须放独立类型 |
| `PhaseGateState` 无窗口计数器（只有 slo/lcb 连击、interventions、risky、elapsed、drift、degradation、evidence_digest） | L128-143 | 窗口终点判定需要新增计数来源；该模型是运行时状态、不落盘、无 digest property，加带默认值字段不触碰证据链 |
| 判定顺序固定：safety(degradation) → identity drift → `budget.max_interventions` → `budget.wall_clock_seconds` →（仅 v1：`budget.risk_quota`）→ SLO → convergence | L171-241, L244-308 | 窗口预算的插入位置必须在设计中显式冻结（见 §6.3） |
| v1 结束门禁的 risk_quota 用严格大于 `>`（L214），而执行前门禁 `evaluate_intervention_gate` 用 `>=`（`intervention.py` L554-556） | — | 既有比较符不一致（相邻事实，本任务不修）；窗口预算推荐 `>=`，与 `max_interventions` 及执行前门禁一致 |
| `GateDecision` 约束：`stop=true` 必须携带 `stop_class` + `triggered_field`；`contract_digest` + `evidence_digest` 必填 | L145-168 | 窗口终点决定天然可套用：`BUDGET_EXHAUSTED` + `budget.max_windows`，模型层零改动 |
| `StrictModel` 为 `extra="forbid"`（`contracts.py` L11-12） | — | 旧 payload 不容忍新字段；新字段只能出现在新 schema 版本里 |

### 2.2 循环侧（`packages/core/looper_core/system_opt/dynamic_loop.py`）

| 事实 | 位置 | 设计含义 |
|---|---|---|
| `max_windows` 是 `run_dynamic_phase` / `run_dynamic_phase_v2` 的函数参数，仅做 `< 1` 校验；不进任何 digest，也不落盘进 run JSON | L241, L260-261, L524, L543-544, L618 | 问题①的直接根源：运行结束的证据里**读不回当时的窗口预算** |
| 循环 `for index in range(1, max_windows + 1)`；每窗结束 `evaluate(window.digest)`，`decision.stop` 即 return | L301, L493-502 / L618, L899-900 | 窗口预算若进 evaluate，耗尽停在第 N 窗内，天然绑定末窗 digest |
| 耗尽后 `final = evaluate(gate_state.evidence_digest)`——**用未变的状态重评一次**，若无停止类触发即 `stop=false` | L504-514 / L902-906 | 问题②的直接根源 |
| **新发现**：`gate_state.evidence_digest` 初始化为 `contract.digest` 且循环内从不更新（逐窗 evaluate 只在副本上覆盖）；因此耗尽后 final decision 的 `evidence_digest` == **workload contract digest**，而非末窗观测 digest | L285-294, L467-481 / L569-578, 868-883 | 已用真实工件验证（§2.7）。即使将来 stop 翻转，这个"终点证据指向合同而非末窗"的弱绑定也必须一并修 |
| 两个 run schema：`v1alpha1` / `v1alpha2`，`load_dynamic_phase_run` 按 schema 分发 | L81-82, L222-232 | run 侧同样只能加新版本分支 |
| run 记录 `gate_contract_digest`，GateDecision 记录 `contract_digest`（二者相等） | L117-118, L155 / L189, L194 | 停止阈值若进 gate 合同，run 已经携带其 digest 引用——回放单源可核验 |

### 2.3 CLI 侧（`services/api/looper_api/cli.py`，`dynamic-run` 自 L1221）

| 事实 | 位置 | 设计含义 |
|---|---|---|
| `--max-windows` 为必填、`min=1`（typer 层已拒绝 0/负数），原样透传给循环 | L1238, L1602, L1623 | 问题④：CLI 与合同两处声明窗口预算；负测矩阵的 0/负例现状是两层拒绝 |
| 会话代际配对：v1 gate + v1 proposals = legacy；v2 gate + v2 proposals = durable；混代拒绝；`--online-routing` 仅 durable | L1303-1314 | 引入 v3 gate 需要同步定义代际配对规则（proposals 是否也 v3 化，见 §8 待裁决 5） |
| 输出 = `run.model_dump(mode="json")` 原样落盘；summary 打印 `stop` / `stop_class` / `stop_reason` | L1826, L1829-1865 | run schema 不加字段，证据里就永远没有窗口预算 |
| lease 约束仅绑定 `gate_contract.budget.wall_clock_seconds` | L1323-1326 | 若窗口预算进合同，lease 校验不需要变（窗口数不受 lease 语义约束） |
| `m3-demo` 固定传 `max_windows=6` | L400 | demo 是"双来源"现状的活例，实现时须同步 |
| **相邻发现**：`dynamic-reactivate` 用 v1 `DynamicPhaseRun.model_validate` 载入 run——**v2 run payload 会被拒绝**（已实测 ValidationError） | L2079 | 既有不对称，属 D5/回放 lane，本任务只登记不修 |

### 2.4 demo/工装侧（`packages/core/looper_core/system_opt/dynamic_demo.py`）

- `build_demo_gate_contract` 固定产出 v1 合同（L186-203）；`build_m3_demo_session`
  用 v1 dump **仅换 `schema_version` 字符串**派生 v2 合同（L331-339）。若共享
  `PhaseBudget` 被改动，这条派生链连带破坏——再次佐证窗口预算必须走独立类型。
- `docs/system-optimizer/contracts/dynamic-session-files.md` L13 仍写
  "gate-contract.json # DynamicPhaseGateContract（model_dump_json）"（v1 表述），
  而真实会话写入的已是 v1alpha2——**合同文档已滞后一代**，任何方案都要顺带更新。

### 2.5 执行前门禁（`intervention.py`）

- `InterventionGateContract` 是 Protocol：`single_change_per_window` +
  `budget: PhaseBudget` + `digest`（L69-76）。若新 budget 类型是
  `PhaseBudget` 的子类，v3 合同**结构兼容、无需改该文件**；若平行新类，
  Protocol 的 `budget` 注解需要放宽（进入写集合）。
- v2 语义下 risk_quota 只在执行前门禁（`evaluate_intervention_gate`），结束
  门禁不参与。因此"窗口上限与风险预算同窗竞争"在 v2+ 实际上是：**执行前
  risk quota 拒绝发生在该窗 `evaluate` 之前**，窗口耗尽根本走不到 evaluate。
  状态迁移表（§7）必须把这条先行路径列出来。

### 2.6 测试覆盖面（只扫文件与关键断言，未运行）

- `tests/test_system_opt_phase_gate.py`（10 tests）：固定优先级、digest 决定性、
  停止决定必须引用 class/field、继续决定不得引用——v3 evaluator 可直接复用该
  断言骨架。
- `tests/test_system_opt_dynamic_loop.py`（7 tests）：**L237-238 把"窗口耗尽
  stop=false + note"钉成了期望行为**（`assert not run.stop_gate_decision.stop`
  + `"window budget" in run.note`）——实现任务中此断言的走向取决于方案选择。
- `tests/test_system_opt_dynamic_v2.py`（5 tests）：v2 gate fixture 由 demo v1
  派生（L100-113）；L258 钉死 `budget.risk_quota` 执行前拒绝。
- `tests/test_system_opt_dynamic_cli.py`（20 tests）：L348 有改写 `--max-windows`
  的用法（依赖参数存在）；L800-802 弱断言"窗口预算收尾时 triggered_field !=
  convergence.rounds"。
- `tests/test_system_opt_dynamic_e2e.py`（2 tests）/ `test_system_opt_dynamic_replay.py`
  （回放为 O1/O2 collection 证据回放，不重评 gate）/ `test_system_opt_dynamic_reactivate.py`
  （3 tests，v1 run）。

### 2.7 本设计的实证记录（2026-08-23，用当前代码 + 真实工件）

- 载入真实 v2 `gate-contract.json` → 重算 digest =
  `sha256:c39720e7d9ba817ab1d363c12b8085c6e04afbb511da8605935735787f735dc0`
  与 run 里记录的 `gate_contract_digest` **逐字节一致**（现状零漂移基线）。
- 载入真实 v2 `dynamic-run.json` → `load_dynamic_phase_run` 成功、run digest
  与 canonical 重算一致、`stop=false`、6 窗。
- final decision 的 `evidence_digest` == `workload_contract_digest`
  （`beb2a9e6…`），**不等于**末窗观测 digest（`933843b4…`）——证实 §2.2 新发现。
- v2 run payload 喂 `DynamicPhaseRun.model_validate`（dynamic-reactivate 的
  载入方式）→ ValidationError。证实 §2.3 相邻发现。

## 3. 必须解决的四个问题（映射到方案评估维度）

| # | 问题 | 现状根源 | 评估维度 |
|---|---|---|---|
| ① | `max_windows` 未进 digest | 仅函数参数（§2.2） | 停止阈值是否被某个 canonical digest 绑定 |
| ② | 运行结束却 `stop=false` | 耗尽后原状态重评（§2.2） | 正常返回的 run 是否必有 `stop=true` 的可回放决定 |
| ③ | 旧证据（v1/v2 payload）回放兼容 | `StrictModel extra=forbid` + 共享 `PhaseBudget`（§2.1） | 旧 payload 载入成功率、digest 重算零漂移、行为语义是否被改写 |
| ④ | CLI 参数与合同重复来源 | `--max-windows` 必填 + （若合同化后）合同字段（§2.3） | 窗口预算的单一事实源；冲突时 fail-closed |

**红线**（handoff §3）：禁止产生未被 digest 绑定的停止阈值；CLI 参数必须与
合同一致或由合同唯一提供。

## 4. 方案 A：gate schema `v1alpha3`，窗口预算纳入新 `PhaseBudgetV3`

### 4.1 schema 形态

- 新增 `DYNAMIC_PHASE_GATE_V3_SCHEMA = "looper.dynamic-phase-gate/v1alpha3"`。
- 新增 `PhaseBudgetV3(PhaseBudget)`：继承三字段，增加
  `max_windows: int = Field(ge=1)`（**必填、无默认**——新合同必须显式声明，
  杜绝隐式阈值）。
- 新增 `DynamicPhaseGateContractV3`：结构与 V2 相同但 `budget: PhaseBudgetV3`。
- `evaluate_phase_gate_v3`：在 `budget.wall_clock_seconds` 之后、SLO 之前插入
  `state.windows_observed >= contract.budget.max_windows` →
  `BUDGET_EXHAUSTED / budget.max_windows`（比较符 `>=`，与 max_interventions
  及执行前门禁一致）。固定顺序变为：
  safety → identity → max_interventions → wall_clock → **max_windows** → SLO → convergence。
- `PhaseGateState` 增加 `windows_observed: int = Field(default=0, ge=0)`
  （运行时状态、不落盘、无 digest，带默认不破坏任何既有构造点；实现时需
  全仓确认构造点清单，见 §9 写集合注）。
- `load_dynamic_phase_gate` 增加 v3 分支；v1/v2 分支与模型类**一字节不动**。

### 4.2 循环与停止证据

- `dynamic_loop.py` 新增 v3 运行路径（新函数或参数化现有 v2 函数）：
  - 窗口预算来自 `gate_contract.budget.max_windows`，不再接受独立 `max_windows`
    实参（类型层消灭重复来源）；
  - 每窗结束后 `windows_observed` 计数进 `gate_state`，耗尽判定发生在**末窗
    的常规 evaluate 内**，`evidence_digest` = 末窗观测 digest（顺带修掉 §2.2
    的"终点证据指向合同"弱绑定）；
  - v3 路径**不再有** "window budget reached" 的 post-loop 兜底重评——循环
    必然以某个 `stop=true` 决定返回。
- run 记录沿用 `DynamicPhaseRunV2`（run schema 不升版）：`GateDecision` 已
  参数化 `contract_digest` / `triggered_field`，而 run 已携带
  `gate_contract_digest` → 回放者拿 run + gate 合同两份文件即可核验
  "阈值在合同里、决定引用合同字段、证据指向末窗"。**这是 A 案的核心收益：
  停止证据的绑定源与合同 digest 同源。**

### 4.3 CLI

- v3 会话：`--max-windows` **禁止提供**（提供即 `BadParameter`；预算唯一来源
  是 gate-contract.json）。fail-closed 消灭双来源，优于"提供且必须相等"
  （相等校验仍是双来源）。
- v1/v2 会话：`--max-windows` 保持必填（否则存量会话无法运行）——参数变为
  条件必填：v1/v2 缺失报错、v3 提供报错。
- `durable_session` 判定扩展为"v2 或 v3 gate + 对应代 proposals"；代际混配
  仍拒绝（proposals 是否需要 v3 化见 §8 待裁决 5）。

### 4.4 代价与风险

- schema 族 +1（v1/v2/v3 三代并存），loader 与 CLI 分支增多；
- 存量 v2 会话脚本需要迁移到 v3 才能获得显式终点（v2 语义保持，见 §7）；
- `PhaseBudgetV3` 必须以继承而非平行新类实现，否则 `InterventionGateContract`
  Protocol 的 `budget: PhaseBudget` 注解要放宽（写集合 +1 文件）。

## 5. 方案 B：run schema `v1alpha3`，运行记录单独绑定 `max_windows` + 版本化停止证据

### 5.1 schema 形态

- gate 合同族完全不动；新增 `DynamicPhaseRunV3`：
  - `max_windows: int = Field(ge=1)`（进 run digest）；
  - `stop_gate_decision` 仍是 `GateDecision`；窗口耗尽时循环直接构造
    `stop=true / BUDGET_EXHAUSTED / triggered_field="max_windows"`，
    `evidence_digest` = 末窗观测 digest；
  - run JSON 里能读回当时预算（解决问题①的 run 侧表达）。
- `load_dynamic_phase_run` 增加 v3 分支。

### 5.2 语义主张与弱点

- 语义主张：`max_windows` 本来就是**运行参数**而非门禁合同——预算记录在
  运行证据里更贴切，且不动 gate 族、改动面看起来更小。
- 弱点 1（红线擦边）：`GateDecision.contract_digest` 指向的 gate 合同里
  **没有** `max_windows` 字段；`triggered_field="max_windows"` 引用的是 run
  自身字段。形式上"阈值被 run digest 绑定"可以辩护，但决定与合同 digest
  异源——审计者从停止决定单独出发核验不到阈值，必须再取 run 全文。与 handoff
  "禁止未被 digest 绑定的停止阈值"的**字面**相符、与"digest 绑定在 gate
  contract"的**推荐方向**相悖。
- 弱点 2：窗口终点判定逻辑留在循环层而非 evaluator 内——"上限 vs SLO 同窗
  竞争"的优先级由代码执行顺序而非合同+固定判定函数表达，可回放性弱于 A。
- 弱点 3：判定仍需感知计数。无论 A/B，`PhaseGateState` 或等价计数都绕不开；
  B 把判定放循环里则 evaluator 测试（`test_system_opt_phase_gate.py` 骨架）
  覆盖不到窗口终点。

### 5.3 CLI

- `--max-windows` 保持必填（预算唯一来源仍是 CLI → 运行参数 → run 记录）。
  问题④在 B 案下的解法是"CLI 是唯一来源、run 落盘绑定"——合同侧零重复，
  但代价是停止阈值与 gate digest 解耦（弱点 1）。

## 6. 方案 C：现 v2 临时兼容——耗尽时直接返回 stop=true 的代价分析

### 6.1 形态

不改任何 schema：在 `run_dynamic_phase_v2` 耗尽处把 post-loop 重评替换为直接
构造 `GateDecision(stop=True, BUDGET_EXHAUSTED, triggered_field="max_windows",
reason="window budget reached …")`。v1 是否同改需拍板（同改则
`test_system_opt_dynamic_loop.py` L237 断言翻转、`dynamic-reactivate` 的 v1
输入语义变化；不同改则缺口只修一半）。

### 6.2 代价（逐条）

1. **违反红线**：此刻 run schema 里没有 `max_windows` 字段、gate 合同里也没有
   ——`triggered_field="max_windows"` 引用一个**任何 digest 都没绑定的阈值**，
   正是 handoff 明令禁止的形态。除非同时把 `max_windows` 落进 run 记录，而
   那已经是方案 B。
2. **语义撒谎**：`contract_digest` 指向的合同不含该字段，回放者无法核验
   "当时预算是多少"，`triggered_field` 形式合法（`GateDecision` 校验只查
   非空）但内容不可证。
3. **行为漂移**：v2 的"同输入重跑"结果从 stop=false 变 stop=true——已落盘
   证据不受影响（不重算），但新旧代码对同一会话产出不同证据，历史可比性
   受损；`unfinished-task-queue` L132 要求"v2 到窗上限必须 stop=true **且引用
   合同字段**"，C 只能满足前半句。
4. **唯一收益**：改动最小（单文件数行）、REAL-M3-01 类表象立即消除、无
   schema 迁移成本。作为**止血补丁**有其价值，但按 1-3 不能作为终态。

## 7. 兼容表与状态迁移表（交付门 1、2）

### 7.1 v1/v2 历史 digest / loader 兼容表

判定口径：旧 payload 在**新代码**下 (a) 能否载入、(b) 重算 digest 是否与
落盘值一致（零漂移）、(c) 重放/重跑行为语义是否与旧代码一致。

| 旧 payload（真实存在） | 案 A | 案 B | 案 C |
|---|---|---|---|
| v1 gate JSON（`dynamic-phase-gate/v1alpha1`） | ✅ 载入；✅ digest 零漂移（共享模型未动）；✅ 语义不变 | ✅ / ✅ / ✅（gate 未动） | ✅ / ✅ / ✅（gate 未动） |
| v2 gate JSON（`v1alpha2`，含 REAL-M3-01 真实合同 `c39720e7…`） | ✅ 载入；✅ 零漂移（`PhaseBudgetV3` 独立，`PhaseBudget` 未动）；✅ 语义不变 | ✅ / ✅ / ✅ | ✅ / ✅ / ✅ |
| v1 run JSON | ✅ 载入；✅ run digest 零漂移；✅ 语义不变 | ✅ / ✅ / ✅ | ✅ / ✅；⚠️ 语义**若**同步改 v1 则 stop 翻转（需拍板） |
| v2 run JSON（含 REAL-M3-01 两相位） | ✅ 载入；✅ 零漂移；✅ 语义不变（v2 会话仍可原样重跑出 stop=false） | ✅ / ✅ / ⚠️ 同一输入重跑 stop 翻转 true（落盘证据不受影响） | 同左 ⚠️ |
| 真实工件回归fixture（建议） | 三案都应把 `.artifacts/real-demo-2026-08-25/...` 的 gate/run payload 固化为测试：载入 + 重算 digest == `c39720e7…` / 与 run 落盘 digest 一致 | 同左 | 同左 |

漂移机理备忘（为什么"给共享 `PhaseBudget` 加字段"不在任何案里）：
必填字段 → 旧 payload 缺字段 ValidationError（loader 断）；带默认字段 →
`model_dump(exclude_none=False)` 含新字段、canonical 重算 digest 变化 →
`dynamic-reactivate` 的 `contract_digest` 相等校验（cli.py L2083-2089）对旧
证据永久失败。因此**所有方案都禁止触碰 v1/v2 共享模型**。

### 7.2 状态迁移表：现 v2 payload / 会话在新代码下的行为

行 = 会话与结局场景；"新代码"分别按案 A（v3 会话）/ 案 B（run v3）/ 案 C。

| 场景 | 现行为（`8439654`） | 案 A 后 | 案 B 后 | 案 C 后 |
|---|---|---|---|---|
| v2 会话，窗耗尽，无其它停止类 | `stop=false`，note=window budget reached，final 证据=workload 合同 digest | **不变**（v2 合同无字段，语义冻结；显式终点仅 v3 会话提供） | run v3 化后新跑 stop=true；旧 v2 payload 不变 | 新跑 stop=true（阈值未绑定，红线违规） |
| v2 会话，末窗 SLO hold 同时达成 | SLO 先判 → `stop=true` TARGET_MET（窗口耗尽不影响，因为每窗 evaluate 在前） | 不变 | 不变 | 不变 |
| v2 会话，末窗前某窗干预计划 risk quota 触顶 | 执行前门禁 stop（`budget.risk_quota`，GATE_REJECTED 窗记录），循环提前 return——**窗口耗尽判定根本不参与** | 不变（v3 同序：执行前门禁先于当窗 evaluate） | 不变 | 不变 |
| v3 gate 会话（案 A），窗耗尽，无其它停止类 | —（v3 不存在） | 末窗 evaluate 即 `stop=true`，BUDGET_EXHAUSTED，`budget.max_windows`，证据=末窗 digest；无 post-loop 兜底 | —（B 无 v3 gate） | — |
| v3 会话，末窗同时窗口耗尽 + SLO hold 达成 | — | budget 类先判 → `budget.max_windows` 赢（固定顺序冻结，负测钉死） | 循环层先构窗口决定 → 同样 budget 赢，但顺序由代码非合同表达 | — |
| v3 会话，末窗同时安全退化 + 窗口耗尽 | — | safety 第一 → `degradation` 赢并要求回滚 | 同左 | — |
| v3 会话，`max_interventions` 与窗口耗尽同窗 | — | max_interventions 在前 → 先触发（既有顺序保持） | 同左 | — |
| `dynamic-run --max-windows 0 / 负数` | typer `min=1` BadParameter；core 层 `<1` ValueError 双保险 | v1/v2 会话同现状；v3 会话参数被禁 → 提供任何值即 BadParameter | 同现状（参数仍必填） | 同现状 |
| `dynamic-reactivate` 喂 v2 run | ValidationError（v1-only loader，既有缺陷） | 不变（相邻缺陷另行立项） | 不变 | 不变 |

### 7.3 窗口预算判定插入位置（案 A 冻结建议）

`safety → identity → max_interventions → wall_clock → **max_windows** → SLO → convergence`

理由：保持既有分层"安全/负载失效最先、预算类先于目标类、收敛最后"；窗口
预算与 wall_clock 同为"任务声明的资源上限"，紧随其后；先于 SLO 意味着
"最后一窗恰好达标"不产生 TARGET_MET（预算先到先停，防追噪）。此位置为
设计建议，最终以用户确认为准（§8 待裁决 4）。

## 8. 负向测试矩阵（交付门 3；实现于 DYN-END-01I）

| # | 输入 / 场景 | 期望（fail-closed 方向） | 案别 |
|---|---|---|---|
| N1 | CLI `--max-windows 0` / `-1` | `BadParameter`（typer min=1）；core 层 `ValueError`（`run_dynamic_phase*` 的 `<1` 校验）——两层都钉死 | 全部 |
| N2 | core 直调 `max_windows=0` | `ValueError("max_windows and probe_top_k need >=1…")` | 全部 |
| N3 | v3 gate payload 缺 `budget.max_windows` | `ValidationError`（必填、无默认） | A |
| N4 | v3 gate `budget.max_windows=0` | `ValidationError`（`ge=1`） | A |
| N5 | CLI v3 会话 + 提供了 `--max-windows` | `BadParameter`（预算唯一来源是合同） | A |
| N6 | CLI v1/v2 会话 + 缺 `--max-windows` | `BadParameter`（存量会话参数仍必填） | A |
| N7 | `max_windows=1`，首窗无任何其它停止类 | 第 1 窗 evaluate 即 `stop=true`，`budget.max_windows`，windows 恰 1 条；无 post-loop 兜底记录 | A/B |
| N8 | `max_windows=3`，无其它停止类 | 恰 3 窗、`stop=true`、`triggered_field="budget.max_windows"`、`stop_gate_decision.evidence_digest == windows[-1].observation_window_digest`（不许再引用 workload 合同 digest——钉死 §2.2 弱绑定的修复） | A/B |
| N9 | 末窗 SLO hold 达成 + 窗口耗尽同窗 | 必须报 `budget.max_windows`（BUDGET_EXHAUSTED），不是 TARGET_MET——固定优先级不漂移 | A/B |
| N10 | 末窗安全退化 + 窗口耗尽同窗 | 必须报 `degradation`（SAFETY_TRIGGERED） | A/B |
| N11 | 末窗 identity drift + 窗口耗尽同窗 | 必须报 `identity_drift_action`（WORKLOAD_VANISHED） | A/B |
| N12 | `max_interventions` 与窗口耗尽同窗 | 必须报 `budget.max_interventions`（既有顺序在前） | A/B |
| N13 | `wall_clock_seconds` 与窗口耗尽同窗 | 必须报 `budget.wall_clock_seconds` | A |
| N14 | 末窗前执行前 risk quota 触顶（v2/v3） | 停在 `budget.risk_quota`（执行前门禁），窗口耗尽不参与——证明先行路径 | 全部 |
| N15 | 真实工件回归：载入 REAL-M3-01 的 v2 gate/run payload | 载入成功；gate 重算 digest == `sha256:c39720e7…`；run 重算 digest 与落盘一致——**历史零漂移的最高强度证据** | 全部 |
| N16 | v1/v2 gate fixture（demo 生成 + 手写 v1）载入重算 | digest 与落盘一致（v1 分支同样零漂移） | 全部 |
| N17 | 伪造 v3 run：`triggered_field="budget.max_windows"` 但 `gate_contract_digest` 指向无该字段的 v2 合同 | 该组合不可由生产代码产生（负测证明构造路径不存在，或回放校验拒绝） | A |
| N18 | 窗口预算停止后，`reactivation_holdout_windows` 语义 | 与其它 BUDGET_EXHAUSTED 停止一致（防振荡保持窗照常；不因预算停止而缩短） | A/B |
| N19 | `stop=false` 继续决定不携带 class/field（既有约束回归） | `GateDecision` 校验继续生效（v3 evaluator 不破坏既有不变量） | A |
| N20 | 案 C（若被选为止血）：新跑 v2 窗耗尽 | `stop=true` 但**必须**同时在 run 记录 `max_windows`（否则红线违规测试直接失败——把红线写成测试） | C |

## 9. 精确写集合（交付门 4；均属 DYN-END-01I，本文档不动）

**共同纪律**：v1/v2 共享模型（`DynamicPhaseGateContract(V2)`、`PhaseBudget`、
`DynamicPhaseRun(V2)`、`DynamicWindowRecord(V2)`、`GateDecision`）在所有方案
中零修改；不迁移旧 evidence；不放宽 D2；不修改业务 SLO/LCB 公式。

### 案 A 写集合

| 文件 | 改动 |
|---|---|
| `packages/core/looper_core/system_opt/phase_gate.py` | `DYNAMIC_PHASE_GATE_V3_SCHEMA`、`PhaseBudgetV3`、`DynamicPhaseGateContractV3`、`evaluate_phase_gate_v3`、`PhaseGateState.windows_observed`（默认 0）、`load_dynamic_phase_gate` v3 分支、`__all__` |
| `packages/core/looper_core/system_opt/dynamic_loop.py` | v3 运行路径（预算取自合同、末窗 evaluate 停止、删除该路径 post-loop 兜底）；`windows_observed` 计数 |
| `packages/core/looper_core/system_opt/dynamic_demo.py` | demo/m3 会话升级为 v3 合同（或并存 v2/v3 两套，见待裁决 5）；`build_m3_demo_session` 派生链适配 |
| `services/api/looper_api/cli.py` | `dynamic-run`：v3 会话分支、`--max-windows` 条件必填/禁用校验、代际配对扩展；`m3-demo` 调用点适配 |
| `packages/core/looper_core/system_opt/intervention.py` | **仅当** `PhaseBudgetV3` 无法以子类满足 Protocol 时改 `InterventionGateContract` 注解（首选不改） |
| `docs/system-optimizer/contracts/dynamic-session-files.md` | gate-contract.json 版本说明、max_windows 单一来源声明（顺带修 v1 表述滞后） |
| `docs/system-optimizer/architecture/workload-tuning.md`、`overall.md` | S10/动态循环行补 v3 语义（一行级） |
| `tests/test_system_opt_phase_gate.py` | v3 evaluator 全套（优先级 N9-N13、N19、digest 决定性） |
| `tests/test_system_opt_dynamic_loop.py` 或新 `test_system_opt_dynamic_v3.py` | N7/N8/N17/N18 + `PhaseGateState` 计数 |
| `tests/test_system_opt_dynamic_cli.py` | N1/N5/N6 + summary 字段 |
| 新 fixture | N15/N16 真实工件回归（payload 拷入 tests fixtures，注意不含敏感信息——工件来自自有授权目标） |

### 案 B 写集合

`dynamic_loop.py`（`DynamicPhaseRunV3` + `max_windows` 字段 + 窗口耗尽决定
构造 + loader v3 分支）、`cli.py`（run v3 产出/透传）、`dynamic_demo.py`、
`contracts/dynamic-session-files.md`（run 文件版本说明）、对应 tests
（N1/N2/N7-N12/N14-N16/N20）。gate 侧零文件。

### 案 C 写集合

`dynamic_loop.py`（v2 耗尽分支替换 post-loop 重评；v1 是否同改待裁决 3）、
`tests/test_system_opt_dynamic_loop.py`（L237 断言翻转）、`contracts/` 文档
一行。**附条件**：若按红线要求把 `max_windows` 落进 run，则范围扩大为案 B。

## 10. 三案对比与推荐（交付门 5）

### 10.1 对比表

| 维度 | A：gate v1alpha3 | B：run v1alpha3 | C：v2 临时兼容 |
|---|---|---|---|
| 问题① max_windows 进 digest | ✅ 进 gate 合同 digest（推荐绑定源） | ✅ 进 run digest（与停止决定异源） | ❌ 不进任何 digest |
| 问题② 结束却 stop=false | ✅ v3 会话末窗 evaluate 必 stop=true | ✅ 新 run 版本必 stop=true | ⚠️ stop=true 但证据不可核验 |
| 问题③ 旧证据回放 | ✅ 零漂移、零行为改写（§7.1） | ✅ digest 零漂移；⚠️ 同输入重跑行为翻转 | ⚠️ 同左，且 v1 是否同改牵动 reactivate 语义 |
| 问题④ CLI 重复来源 | ✅ v3 下合同唯一来源，提供参数即 fail-closed | ⚠️ CLI 仍是唯一来源（无重复，但阈值与 gate digest 解耦） | ➖ 未解决（CLI 仍是唯一来源，同 B） |
| 红线"停止阈值必须被 digest 绑定" | ✅ 完全满足 | ⚠️ 字面满足、绑定源非合同 | ❌ 违反 |
| 同窗竞争优先级的可回放性 | ✅ 合同字段 + 固定 evaluator 顺序（可复用既有 gate 测试骨架） | ⚠️ 循环层代码顺序 | ⚠️ 循环层代码顺序 |
| 终点证据绑定质量 | ✅ 末窗 digest（顺带修 §2.2 弱绑定） | ✅ 末窗 digest 可一并修 | ➖ 未定义 |
| 改动面 | 大（gate+loop+CLI+demo+docs+tests） | 中（loop+CLI+docs+tests） | 小（loop+tests） |
| schema/迁移成本 | v3 会话需重生成 gate JSON；脚本迁移 | v3 run 自然产生，会话文件不动 | 无 |
| 与 handoff 推荐方向一致性 | ✅ 一致（"优先把窗口预算纳入新 gate schema"） | ⚠️ 偏离推荐方向 | ❌ 偏离（handoff 明示不倾向） |
| 适合角色 | 终态 | 终态备选（若用户裁定 max_windows 属运行参数语义） | 短期止血（须附 run 落盘，实质滑向 B） |

### 10.2 推荐

**推荐方案 A**（gate `v1alpha3` + `PhaseBudgetV3` + CLI 单一来源互斥）。

理由（三条）：
1. **唯一同时闭环四问题且不踩红线的方案**：阈值进合同 digest、末窗 evaluate
   必 stop=true、v1/v2 零漂移（新类型独立继承）、v3 下合同是预算唯一来源。
2. **停止证据与合同同源**：`triggered_field="budget.max_windows"` +
   `contract_digest`（run 已携带），回放者两份文件即可完整核验，且固定判定
   顺序仍在 evaluator 内——`test_system_opt_phase_gate.py` 的既有断言骨架
   （安全压一切、继续决定不带 class 等）直接复用于 v3。
3. **方向一致**：handoff §3 已写明推荐方向"优先把窗口预算纳入新 gate
   schema；禁止产生未被 digest 绑定的停止阈值"，A 是该方向的直译。

次选 B：仅当用户裁定"`max_windows` 语义上属运行参数而非门禁合同"时采用。
C 不推荐作为终态；若 REAL 线上急需止血，建议按 §6.2 附条件（同时落盘
max_windows）作为受时限的临时补丁，并在其后仍落地 A。

### 10.3 不拍板声明

**本节为推荐，不是决定。** 方案选择、以及下列待裁决点，全部等待用户确认；
确认前不启动 DYN-END-01I，不写任何生产代码：

1. **口径冲突需裁决**：`unfinished-task-queue-2026-08-24.md` L132 写
   "v2 到窗上限必须 `stop=true` 且引用合同字段"。按字面，v2 合同无窗口
   字段，"引用合同字段"在**不改 v2 schema**的前提下不可能成立（改 v2 共享
   模型则 digest 漂移，§7.1 备忘）。两种调和：(a) 该行意图为"修复后的现行
   版本会话"（= 案 A 的 v3）；(b) 坚持字面 v2，则只能选 C+B 混合（v2 行为
   翻转 + run 落盘绑定）并接受"同输入重跑行为漂移"。**需用户明确选 (a)
   或 (b)。**
2. **A 案下存量 v1/v2 会话是否接受语义冻结**（显式终点只对新 schema 承诺，
   旧会话重跑仍是 stop=false + note）。
3. **C 案若作为止血是否同步改 v1 路径**（牵动 `test_system_opt_dynamic_loop.py`
   L237 断言与 `dynamic-reactivate` 的 v1 输入语义）。
4. **窗口预算在判定顺序中的插入位置**（§7.3 建议 wall_clock 之后、SLO 之前）。
5. **v3 会话的代际配对**：v3 gate 是否要求 v3 proposals（现 CLI 强制 gate 与
   proposals 同代），还是允许 v3 gate + v2 proposals（proposals 结构本就无
   预算语义，理论上可跨代——但会打破既有同代纪律，默认建议同代升版）。
6. **demo 工装**：`m3-demo`/`build_m3_demo_session` 是否一并切 v3（涉及
   `max_windows=6` 的来源迁移），还是保留 v2 demo 作为 legacy 回放样例。

## 11. 本设计核实过但未决的事项（诚实清单）

- `PhaseGateState` 全仓构造点未逐一枚举（设计假定加默认字段安全；实现时
  DYN-END-01I 必须先 `grep` 构造点并逐一确认，本任务不写代码故未做穷举）。
- `docs/system-optimizer/contracts/dynamic-session-files.md` L13 的 v1 表述
  滞后是**顺带发现**，修复属写集合文档行，不单独立项。
- `dynamic-reactivate` 只吃 v1 run（v2 被 ValidationError 拒绝，已实测）是
  **相邻既有缺陷**，超出本任务边界，建议登记为独立任务（不属 DYN-END-01）。
- v1 结束门禁 risk_quota `>` 与执行前门禁 `>=` 的比较符不一致是**相邻既有
  事实**，本设计不动它（D2/风险预算语义另有归属）。
