# 分层实现规范与目录说明

> 状态：normative implementation spec；配套[总体架构 v2](overall.md)。
> 日期：2026-08-23
> 实现纪律：**自底向上逐层实现；每层测试全绿并过该层验收门禁后，才允许实现更上层。**
> 每个新模块文件头部 docstring 必须标注所属架构层与一句话职责。

## 1. 目录与层的对应

```
packages/core/looper_core/system_opt/
├── executor/                     L0 执行后端（现有：simulated / local_linux / ssh_remote / runner）
├── config_manifest.py            L1 配置清单（现有）
├── state_evidence.py             L1 状态与所有权证据（现有）
├── lease.py                      L1 租约与 fencing（现有）
├── safety.py                     L1 安全执行原语 snapshot/apply/verify/rollback（现有）
├── inventory.py                  L1 配置清点（现有）
├── domain.py                     L1/L5 动态合法域解析 S5（现有）
├── scoring.py                    L2 测量与改善量公式 S6 + 组件优先级 S4 原语（现有）
├── measurement.py                L2 测量命令规格（现有）
├── pressure.py                   L3 压力器：标准压力协议 + 校准（现有；压/采解耦渐进）
├── interference.py               L3/L4 独占窗口进程干扰门禁（现有）
├── collector.py                  L4 采集器（新建，本规范 §3）
├── tuning.py                     L5 组件闭环引擎（现有；终裁权上收 L8 为后续改造）
├── policy.py                     L5 策略合同（现有）
├── profiles.py                   L5 配置档（现有）
├── rollback.py                   L6 回退器（新建，本规范 §4）
├── negative_cache.py             L7 负结果缓存（新建，本规范 §5）
└── engine/                       L8 总引擎（新建，本规范 §6）
    ├── __init__.py
    ├── scorer.py                 打分器：S4 组件优先级 + S6 改善量编排
    ├── judge.py                  判断器：S0 可比 → S2 门禁 → S7 接受
    └── scheduler.py              调度器：S3 路由 + L7 负缓存查询 → 选组件/候选
```

每层一个验收门禁（实现顺序 = 门禁顺序）：

| 序 | 层 | 模块 | 验收门禁（全部满足才进下一层） |
|---|---|---|---|
| 0 | L0–L2 | 现有 | 现有 101 个 system_opt 测试全绿（已满足） |
| 1 | L4 采集器 | collector.py | ✅ 2026-08-23 通过（13 测试：读写约束/fixture 采集/PMU 盲区/不可读非崩溃/digest 稳定） |
| 2 | L6 回退器 | rollback.py | ✅ 2026-08-23 通过（14 测试：四级记录模型/相位恢复三态判定/退化级 S8 占位 fail-closed） |
| 3 | L7 负缓存 | negative_cache.py | ✅ 2026-08-23 通过（17 测试：四分量身份敏感/无证据拒收/JSONL 追加/坏行报错） |
| 4 | L8 引擎 | engine/ | ✅ 2026-08-23 通过（11 测试：打分排序/判断器否定理由完整/调度器缓存跳过与显式耗尽） |
| 5 | L5 改造 | tuning.py 等 | 组件内终裁降格为上报引擎（单独后续阶段，不在本轮） |

## 2. 全层通用规范

1. 一切模型继承 `StrictModel`（拒绝未知字段）；摘要用 `canonical_digest`。
2. schema_version 命名 `looper.<domain>/v1alpha1`；只增不删。
3. fail-closed：缺证据、单位不符、身份不一致 → 显式失败或显式状态，绝不静默。
4. 每个公开函数/模型可独立测试，禁止模块级单例副作用。
5. 时间戳一律 `datetime.now(UTC)`。

## 3. L4 采集器规范（collector.py）

职责：在 L3 构建的负载下按组件采集指标快照，为 S1 基线校准、S4 组件优先级、
S6 改善量供数。不做判定、不评价收益。

### 3.1 Guest 盲区契约（CVM 内不可读指标，必须显式处理）

- 每个指标条目必须携带 `availability`：
  - `readable`：值必须为有限正/负数（按指标语义），来源路径必须记录；
  - `unavailable`：值必须为 `null`，必须携带 `unavailable_reason`（如
    "guest 无 /sys/bus/event_source/devices 条目（PMU 未透传）"）。
- **禁止把不可读当 0、当缺失、当猜测值**；不可读本身是有效证据。
- PMU/硬件计数器：默认探测 `/sys/bus/event_source/devices/` 与
  `/proc/sys/kernel/perf_event_paranoid`；guest 未透传时该类指标整体标记
  `unavailable` 并注明探测依据。
- 快照记录 `counting_basis`（采集口径）与探测过的不可读项清单。

### 3.2 数据模型

- `MetricAvailability`、`CollectedMetric`（name/unit/value/availability/
  unavailable_reason/source，读写约束见上）、`ComponentMetricSnapshot`
  （component/target_id/environment_digest/collected_at/metrics/
  counting_basis + digest）。
- 组件枚举与 L5 一致：cpu / memory / storage / network / numa。

### 3.3 采集函数（根目录可注入，Windows 可测）

- `collect_component_snapshot(component, ..., proc_root, sys_root, interval_seconds)`。
- cpu：两次 `/proc/stat` 采样算 busy-ratio；附 PMU 可用性探测。
- memory：`/proc/meminfo` 的可用比。
- network：`/proc/net/dev` 聚合收发计数器。
- storage：`/proc/diskstats` 聚合 I/O 计数器。
- numa：`/sys/devices/system/node/` 节点计数（0 → unavailable 证据）。

### 3.4 验收门禁

1. 读写约束双向测试（readable 带值 / unavailable 带理由，违反即拒）；
2. fixture procfs/sysfs 树上各组件采集正确性；
3. PMU 未透传 fixture → hardware 指标 unavailable 且带探测依据；
4. 不可读文件 → unavailable（不是异常崩溃、不是 0）；
5. digest 稳定性（同输入同摘要）。

## 4. L6 回退器规范（rollback.py）

职责：四级回退的编排与证据。回退动作本身经 L1 安全底座执行。

| 级 | 触发 | 实现 |
|---|---|---|
| a 候选级 | 每个候选测完 | 现有（tuning 闭环内 safety rollback），本模块只登记记录模型 |
| b 相位级 | 搜索结束无优化（S10 停止） | `verify_phase_restoration(actual, baseline)`：快照 digest 全等才算恢复；不等 → `needs-attention` |
| c 退化级 | 已采纳变更后续退化（依赖 S8 的 U_regression，未实现） | **合同占位**：模型与触发字段先定义，执行体等 S8；禁止提前假装实现 |
| d 崩溃级 | 进程崩溃/租约过期 | 现有 reconcile-expired-lease / recover-attention 路径，本模块登记引用 |

验收门禁：相位级恢复判定（全等/不等/缺项三态）纯逻辑测试；四级记录模型
schema 测试；退化级占位显式标注"依赖 S8"。

## 5. L7 负缓存规范（negative_cache.py）

职责：登记并查询"未能优化到的指标/候选"，供 L8 调度器跳过已证无效的尝试。

### 5.1 身份键与失效

`identity = canonical_digest(environment_digest, candidate_parameters,
pressure_protocol_digest, formula_versions_digest)` —— 四分量任一不同即为
不同键；**任一分量变化自动 miss**（不设跨环境信任）。

### 5.2 红线（实现必须强制）

1. **每条目必须挂至少一个证据 digest**（optimization-run / measurement-batch
   其一）；无证据条目在校验层直接拒绝，不写入。
2. **append-only**：存储为 JSONL 追加；加载时逐条校验，坏行报错不跳过。
3. 缓存的是证据不是结论：verdict 必须是显式枚举
   （`no-improvement-lcb` / `gate-rejected` / `stability-rejected`），
   附 metric_id 与记录时间。

### 5.3 验收门禁

同身份命中、异身份未命中、任一分量变化未命中；无证据条目被拒；
JSONL 往返追加不变更旧行；digest 稳定。

## 6. L8 引擎规范（engine/）

职责：调度、判断、打分三器官；不做测量、不直接写配置、不私藏门禁。
第一版只引用**已实现**公式（S0/S2/S4/S6/S7 + S10 枚举），S3 以组件顺序表
代替（真实路由等 workload 相位）。

### 6.1 scorer（打分器）

- `score_components(...)`：输入各组件观测（L4 快照/测量批次），输出
  `ComponentScore`（组件、S4 优先级向量、Pareto 层、依据 digest）。
- 候选级改善量沿用 S6（`bootstrap_improvement`），不重复实现。

### 6.2 judge（判断器）

- 依固定顺序：S0 可比性 → S2 硬门禁（不可补偿）→ S7 接受（LCB>MDE）。
- 输出 `Verdict{comparable, feasible, accepted, stop_signals, reasons}`；
  每个否定结论必须带理由（哪条公式、哪个输入不满足）。

### 6.3 scheduler（调度器）

- 输入：组件分数 + 各组件候选 + L7 负缓存。
- 输出：本轮 `(component, candidate)` 选择与被跳过清单（含缓存命中键）。
- 全部候选被缓存命中 → 显式返回"无待试候选"，不算错误。

### 6.4 验收门禁

三器官各自单测（含否定理由完整性）；组合冒烟测试（分数→调度→判断闭环，
simulated 数据驱动）；引擎内不得出现新的阈值常量（全部来自任务输入）。
