# 分层实现规范与目录说明

> 状态：normative implementation spec；配套[总体架构 v2](overall.md)。
> 日期：2026-08-23
> 实现纪律：**自底向上逐层实现；每层测试全绿并过该层验收门禁后，才允许实现更上层。**
> 每个新模块文件头部 docstring 必须标注所属架构层与一句话职责。

## 1. 目录与层的对应

层目录已实体化（2026-08-23）：每层一个包，`__init__.py`/模块头写明接口与调用规范：
`executor/`(L0)、`foundation/`(L1 门面)、`measurement/`(L2 门面)、`pressure/`(L3 门面)、
`collector.py`(L4，原地不动待 GPT 新合同)、`component/`(L5，含 `strategy.py` +
`strategies/*.yaml` 五组件优化策略)、`rollback/`(L6)、`negative_cache/`(L7)、
`engine/`(L8)。单文件层升级为同名包，导入路径不变。

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
| 1 | L4 采集器 | collector.py | ✅ 2026-08-23 修复后通过（42 项 L4 测试；原 13 项仅为基础基线，不代表压/采解耦合同完整） |
| 2 | L6 回退器 | `rollback/__init__.py` + `rollback/regression.py` | ✅ 核心执行 2026-08-24 通过（四级记录/相位恢复三态；L6c S8 显式阈值→S9 last-good→L1 精确恢复；CLI 生命周期属 G5） |
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
S6 改善量供数。不做加压、不做判定、不评价收益。

### 3.1 层边界与依赖

- L4 只依赖标准库、Pydantic、`StrictModel`、`canonical_digest` 与现有 L2
  `MeasurementBatch` / `MetricEvidence`；禁止反向依赖 L3、L5、L8、数据库或执行后端。
- `ComponentCollector` 是可替换采集边界；同一 `ComponentCollectionRequest` 可注入不同
  collector。`run_component_collection(..., enabled=False)` 必须不调用 collector，作为采集
  开销 A/B 的关闭组。
- 不修改 L2 schema。L4 用 `CollectionMeasurementEnvelope` 同时封装 collection run、原样的
  `MeasurementBatch` 及其 digest，并在 envelope 中原样保留 unavailable 证据。
- `CollectionOverheadABEvidence` 只保存等长配对的开/关原始耗时；L4 不内置接受阈值、权重
  或收益公式。

### 3.2 Guest 盲区与模型合同

- 每个指标条目必须携带 `availability`：
  - `readable`：值必须是有限标量或非空有限序列，来源必须记录；序列用于保留主指标的
    原始多次观测/分布，不允许只留下派生均值；
  - `unavailable`：值必须为 `null`，并携带 `unavailable_reason`。
- **禁止把不可读当 0、当缺失、当猜测值**；不可读本身是有效证据。
- `ComponentMetricSnapshot` 继续使用既有 v1alpha1 字段形状和摘要序列化口径，同时强制：
  组件只能是 cpu / memory / storage / network / numa；时间戳带时区；字典键等于
  `metric.name`；指标名前缀属于快照组件。
- `ComponentCollectionRequest` 显式绑定 target、environment、工作负载阶段/来源、collector
  身份、请求指标列表、采集窗口、测量身份和资源范围。network 必须给出准确接口列表；
  storage 必须给出准确设备列表；缺少范围时 fail-closed，禁止隐式整机聚合。
- 压力工具原始输出通过 `CollectionInputArtifact` 交给 L4：只登记 artifact_id、source、
  media_type 与 sha256 digest，不在合同里预设 iperf3/fio/sysbench 等格式。指定 collector 负责
  解析并按 `requested_metrics` 精确交付；实际 collector 身份或返回指标集合不一致即拒绝。

### 3.3 内置 Linux guest collector

- 根目录、sleep 与 wall clock 均可注入，允许在 Windows fixture 上验证。
- cpu：两次 `/proc/stat` 采样；总 tick 计 user 到 steal，guest/guest_nice 因已包含于
  user/nice 而不重复计数。CPU PMU 可用性只认 canonical
  `/sys/bus/event_source/devices/cpu`；`perf_event_paranoid` 作为独立事实报告。
- memory：读取 `MemAvailable / MemTotal`，并验证
  `0 <= MemAvailable <= MemTotal` 且 total > 0。
- network：只聚合调用方明确列出的 `/proc/net/dev` 接口（`lo` 可被明确选择）。保留窗口
  结束时累计 total，同时给出窗口 delta 和 rate。
- storage：只聚合调用方明确列出的 `/proc/diskstats` 设备；不自动添加分区，因此不会把
  整盘与分区重复相加。保留窗口结束时累计 total，同时给出窗口 delta 和 rate。
- network/storage 任一计数器下降时，保留可读的结束累计值，但对应 delta/rate 标为
  unavailable；L4 不猜测 reset 或 wrap 规则。
- numa：只报告 guest 可见节点数；节点目录数量不能证明当前 workload 的绑核/绑内存状态，
  所以未提供实际绑定测量时 `numa.binding` 必须 unavailable。

### 3.4 L2 绑定合同

`bind_collection_to_measurement_batch(...)` 把 readable L4 指标转换为单值
`MetricEvidence`（标量成为单值列表，序列完整保留），并可合并到已有 L2 batch（压力工具
主指标、gate、阶段/稳定性证据原样保留）；
不可读证据留在 L4 envelope。`collection_metric_names` 明确标识 L4 注入的指标子集。
envelope 必须校验：

1. collection run 已启用，snapshot 与 request 的 component/target/environment 一致；
2. L2 identity 精确绑定 component、target、environment、collection run digest；
3. `measurement_batch_digest` 与实际 batch 一致；
4. 已有 L2 主指标不得被覆盖；L4 readable 指标子集和 unavailable 指标的集合与值均不得
   被静默删改。

### 3.5 验收门禁

1. readable/unavailable 双向约束、组件/指标身份、时区和旧摘要稳定性；
2. CPU 完整字段算法、畸形输入、canonical PMU 识别与 perf 权限事实；
3. memory 边界；network 显式接口（含 loopback）；storage 精确设备与分区不重复；
4. 网络/存储窗口 total/delta/rate、缺对象和计数器 reset 均 fail-closed；
5. NUMA 不从拓扑推断 workload binding；
6. collector 可替换、关闭开关不调用 collector、run/collector/请求指标身份不匹配拒绝；
7. L2 envelope digest/身份/指标绑定及 unavailable 原样保留；
8. 开销 A/B 原始观察必须成对、有限、非负，不在 L4 设阈值。

## 4. L6 回退器规范（rollback.py）

职责：四级回退的编排与证据。回退动作本身经 L1 安全底座执行。

| 级 | 触发 | 实现 |
|---|---|---|
| a 候选级 | 每个候选测完 | 现有（tuning 闭环内 safety rollback），本模块只登记记录模型 |
| b 相位级 | 搜索结束无优化（S10 停止） | `verify_phase_restoration(actual, baseline)`：快照 digest 全等才算恢复；不等 → `needs-attention` |
| c 退化级 | 已采纳变更后续退化（S8 的 U_regression 低于任务显式阈值） | `rollback/regression.py`：只接受 S9 promoted last-good 完整快照，经 L1 精确恢复；失败 needs-attention；CLI 生命周期接线属 G5 |
| d 崩溃级 | 进程崩溃/租约过期 | 现有 reconcile-expired-lease / recover-attention 路径，本模块登记引用 |

验收门禁：相位级恢复判定（全等/不等/缺项三态）纯逻辑测试；四级记录模型
schema 测试；退化级覆盖 not-triggered、精确恢复、L1 异常、non-kept 与
needs-attention；真实目标演练仍单独验收。

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
