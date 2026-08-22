# Variability Analyzer（性能波动分析器，VGO）

本文描述 Looper 的公共波动分析器：它借鉴 VGO（Variability-Guided Performance Optimization, ICPE '26）的 Evidence → Analysis 思路，回答"性能是否稳定、如何波动、慢运行有什么共同特征、波动可能来自哪里"。

## 1. 定位与边界

Variability Analyzer 不是一个 Benchmark，而是所有 Benchmark 共享的公共分析组件：

```
Experiment Data（统一观测数据）
      │
  ┌───┴──────────┐
  ↓              ↓
BenchTrust     Variability Analyzer
可信度分析       稳定性与波动分析
```

模块边界（不重复建设）：

- **BenchTrust**：这组结果是否足以支持可信结论；
- **VGO（本模块）**：结果是否稳定、如何波动、慢运行关联什么、来源在哪、下一步做什么实验；
- **CloudyBench**：争用、故障与恢复的专项诊断；
- **TailBench++**：动态负载下的请求级尾延迟与 SLO。

VGO 只提供它们都能复用的通用波动分析能力。它读取 Looper 统一后的实验数据（ObservationRecord / AttemptRecord / Run Envelope），不解析任何上游 Benchmark 原始格式，因此 DCPerf、CloudyBench、fio、TailBench、Memory Test 及后续 Benchmark 都能直接使用。

## 2. 分析链

```
重复实验数据
    ↓ 基础分布统计（mean/median/std/CV/p95/p99/CVaR/min/max/IQR/MAD/偏度）
    ↓ 稳定性判断（stable / warning / unstable / insufficient_evidence）
    ↓ 异常运行与快慢运行聚类（IQR fence + 一维 cutoff 双模式检测）
    ↓ 慢运行关联的系统特征（点二列相关 + 均值抬升，表述为关联线索）
    ↓ 可能的波动来源（跨宿主机/placement/日期/时间块/环境的 η² 方差分解）
    ↓ 建议的控制变量或 A/B 实验（线索 → 实验映射）
```

### 2.1 稳定性判定

- 样本数低于策略下限（默认 5）→ `insufficient_evidence`（fail closed，不猜测）；
- CV 超过 `cvUnstable`（默认 0.15）或慢运行占比超过 30% → `unstable`；
- CV 超过 `cvStable`（默认 0.05）、存在 IQR 异常、疑似双峰或显著偏度 → `warning`；
- 其余 → `stable`。

### 2.2 快/慢模式检测（VGO cutoff 的简化版）

对排序后的运行值尝试所有一维切分，取组间方差占比（R²）最大的切分，仅当同时满足以下条件才判定为疑似双峰：

1. R² ≥ 0.5；
2. 两个簇都至少有 2 个样本且不少于总样本的 10%；
3. 簇间空隙 ≥ 2.5 × 簇内标准差（防止把普通抖动误判成模式）。

方向感知：minimize（时延）时慢=高值，maximize（吞吐）时慢=低值。

### 2.3 关联线索（不是因果结论）

对每个系统指标计算"是否慢运行"标签与指标值的点二列相关系数和慢/正常组的均值抬升（lift）。只有同时满足 |r| ≥ 0.3 且 lift ≥ 1.2 的指标才进入线索表，并且：

- 每条线索都明确标注"关联线索，不构成因果结论，需要控制变量实验验证"；
- `cycles`、`cpu_time` 等属于波动**结果**而非原因的指标（VGO 论文的过滤原则）会被标记 `likelyConsequence`，提示谨慎解读。

### 2.4 波动来源归因

按宿主机、placement、日期、时间块、环境（target）分组计算 η²（组间方差占总方差比例），η² ≥ 0.25 的维度标记为主导来源；若每次运行携带多个样本，还会输出"运行内"方差占比。η² 之间可能重叠，输出为排序线索而非互斥分解。

### 2.5 验证建议（线索 → 实验映射）

| 线索 | 建议的控制实验 |
| --- | --- |
| cpu_migration_count 升高 | 固定 CPU affinity（taskset/numactl）后 A/B |
| numa_migration_count 升高 | 关闭 NUMA balancing（sysctl）后 A/B |
| dtlb_miss / page_fault 升高 | 启用透明大页（THP）后 A/B |
| context_switch 升高 | 线程绑核 / isolcpus 隔离后 A/B |
| cache_miss 升高 | 固定内存布局（numactl --membind）后 A/B |
| cpu_frequency_mhz 变化 | 固定 CPU 频率后重测 |
| 宿主机 η² 主导 | 更换宿主机重复实验 |
| 日期 η² 主导 | 增加跨日重复 |
| placement η² 主导 | 更换 placement 重复实验 |
| 无系统指标 | 先补充 perf/系统指标采集 |
| 疑似双模式 | 逐项固定变量做 A/B 排除 |
| 已采集指标 | profiler 开/关各测一轮，排除采集开销 |

只有完成控制实验且波动随变量变化时，才允许把"关联"升级为"原因"——这一步由人工/后续流程判断，分析器本身永远不会输出因果断言。

## 3. 分布比较（超越均值）

`compare_distributions` 同时比较两组配置的：

- mean / median 改善幅度；
- 尾部改善（CVaR95 / P95，方向感知）；
- CV 比值（稳定性变化）；
- 慢运行概率（慢模式 + 慢异常占比）；
- 跨宿主机最差表现（每宿主机中位数的最差值）；
- SLO 超出概率（若实验 gates 中声明了 SLO 阈值）。

结论明确区分五种情形：`dominant`（分布整体占优）、`dominated`、`mean_better_tail_worse`（均值改善但尾部恶化）、`mean_worse_tail_better`、`inconclusive`。对于权衡情形，报告显式输出"不要仅凭均值选择 X；若业务重视尾延迟/SLO 应优先 Y"，把决定权交给用户的稳定性偏好，绝不自动选边。

## 4. 系统指标契约

Benchmark / Adapter 用标准观测名输出系统指标（普通 ObservationRecord 即可，无需新表）：

```
cpu_migration_count, context_switch_count, page_fault_count, dtlb_miss_count,
cache_miss_count, numa_migration_count, cpu_frequency_mhz, cpu_utilization_percent,
iowait_percent, run_queue_depth
```

没有这些指标时分析器仍可完成第一层统计与稳定性判断，并明确建议"补充系统指标采集"。

## 5. 数据流与代码位置

```
ObservationRecord + AttemptRecord + Run Envelope（统一数据）
    → services/api/looper_api/variability_service.py   # 构造 RunSample，按 target/candidate × workload 分组
    → packages/core/looper_core/variability.py         # 纯分析器（可独立测试，无 DB 依赖）
    → AnalysisSnapshotRecord（复用现有快照表，policy_digest 区分于普通分析）
    → GET /api/v1/experiments/{id}/variability
    → apps/web 实验详情页"波动分析" tab
```

- 分组：selection 模式按 target 分组、optimization 模式按 candidate 分组，组内再按 workload；
- 运行身份：host 取自 Run Envelope / target snapshot 的 fingerprint，placement 与 time block 取自 envelope extensions，日期取自 attempt 时间戳；
- 结果持久化为 AnalysisSnapshotRecord，`policy_digest` 绑定分析器版本 + 阈值策略 + 目标指标，同输入同策略命中缓存，不重复计算；
- 前端"波动分析" tab 展示：稳定性结论卡、分布统计、快/慢模式、运行分类条、关联线索表、方差来源条形图、验证建议列表、分布比较卡。

## 6. 策略与可复算性

`VariabilityPolicy` 的全部阈值（CV 门槛、IQR 系数、模式判据、线索门槛、η² 门槛、最小样本数）都进入 policy digest；修改阈值产生新快照而不是覆盖旧结论。分析器版本（`looper.variability-analyzer` / 版本号）记录在每份报告与快照中。

## 7. 当前边界（P0 未覆盖）

- 一维 cutoff 只检测**两个**模式，VGO 论文中的三模态（如 SAM2 分阶段案例）暂不支持；
- 关联用点二列相关，未实现决策树特征重要性与多因子联合分析（论文 Step 3）；
- 未集成 VGO 的"因子 → 缓解措施"知识库与自动施加（taskset/sysctl 自动执行属于后续阶段）；
- 缓解后分布对比（论文 Step 5/6 的 baseline vs mitigated 叠加）需人工发起两次实验后用分布比较功能完成；
- selection 模式的分布比较是同 workload 内 target 两两比较，跨 workload 聚合暂未提供；
- η² 分解的维度间可能重叠，输出为线索排序而非严格的方差分解（ANOVA）。

## 8. 快速验证

```python
from looper_core.variability import RunSample, analyze_variability, compare_distributions

report = analyze_variability(
    [RunSample(runId=f"r{i}", value=v) for i, v in enumerate(values)],
    metric="runtime", unit="second", direction="minimize", group_label="demo",
)
print(report.status, report.distribution.coefficient_of_variation)
```

测试见 `tests/test_variability.py`：模式检测、异常标记、关联线索（含结果指标过滤）、宿主机归因、建议生成、方向感知、均值/尾部权衡、SLO 超出率、服务层快照缓存共 16 项。
