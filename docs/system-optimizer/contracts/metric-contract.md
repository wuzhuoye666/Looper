# 指标契约

> 状态：normative draft  
> 已确认：方向感知、低开销优先、组件内二维优先级、业务与系统指标分工。  
> 未确认：具体字段 schema、计算公式、阈值和采样频率。

## 指标角色

一个指标必须显式声明角色，不能仅凭名称推断：

| 角色 | 用途 | 是否进入整体 workload 收益 |
|---|---|---|
| workload objective | 业务最终目标 | 是，按 workload 合同 |
| hard guardrail | 正确性、安全、SLO 或稳定性门禁 | 不加权；违反即 infeasible |
| component objective | 通用调优中的组件主指标 | 只进入该组件结果 |
| pressure signal | 路由候选组件 | 否 |
| diagnostic | 组件内部解释和假设 | 否 |
| environment | 证明环境和可比性 | 否 |
| trust/evidence | 评价测量有效性 | 作为门禁或单独置信度 |
| cost | 资源、能耗或修改成本 | 只有合同声明时参与决策 |

## 最小字段

后续 schema 至少应覆盖：

- metric_id、source_id、source_locator、raw_name、canonical_name。
- semantic_status：verified、partially-verified 或 unverified。
- role、direction、unit、statistic 和 aggregation window。
- primary_component、related_components。
- scope type 与 scope instance。
- workload phase 和 phase source。
- collection method、collector version、sample frequency。
- collection cost level、measured overhead 和数据丢失状态。
- direct 或 derived；派生指标必须引用原始输入和公式版本。
- comparison reference、normalization policy 和 near-zero policy。
- availability、required/optional 和 missing reason。
- environment、hardware、kernel、tool 和 workload identity。

这些是待落 schema 的字段要求，不代表字段名已最终确认。

## 方向

必须支持：

- maximize：下降是不利变化。
- minimize：上升是不利变化。
- target：远离目标是不利变化。
- range：越出允许区间是不利变化。
- diagnostic-only：不计算优化方向。

禁止仅使用 raw increase 作为统一退化定义。CPU 利用率、内存占用、带宽等上下文相关指标还必须结合排队、压力、作用域或业务结果，不能仅因数值高就定性为瓶颈。

## 当前不利压力

当前不利压力的参考来源必须显式记录，可以是：

- 硬件或软件容量。
- workload SLO。
- 安全界限。
- 同一 workload 同阶段的冻结基线。
- 经实测确认的正常分布。

没有可信参照时只能报告 raw observation，不能生成“高压力”事实。不得设置跨所有指标通用的 80% 等阈值。

## 不利变化

方向感知相对变化可作为正值、非近零指标的候选算法，但不是通用公式。必须同时保存绝对变化和原始值。

以下情况需要专门策略：

- 基线接近零或为零。
- 指标可为负数。
- 目标型或区间型指标。
- 百分位、长尾和明显偏态指标。
- 计数器回绕、采样丢失或重置。
- workload 阶段改变。

近零阈值、分布估计器和时间窗口均为 open decision。

## 采集成本层级

| 级别 | 预期方式 | 启用策略 |
|---|---|---|
| L0 | workload 原生业务输出 | workload 期间按合同采集 |
| L1 | 低开销系统概览和压力 | 持续或低频周期采集 |
| L2 | 组件微指标和有限 PMU 事件 | 下钻触发、限定窗口 |
| L3 | trace、profiling 或高事件量采集 | 明确证据需要和授权后启用 |

“低开销”必须经目标环境 A/B 实测才能成为 verified。设计者估计、工具文档或论文开销只能作为待验证依据。

## 时间与阶段

- 业务指标和系统指标必须具有可对齐时间戳或窗口身份。
- 不同 workload 阶段默认不可直接计算变化。
- 自动阶段识别未实现前，允许 workload manifest 提供阶段标记。
- 采集间隔不同的指标在聚合前必须记录对齐方法，不能静默插值。

## 作用域

至少考虑 host、NUMA node、CPU/core、device、queue、interface、process/thread、cgroup、container 和 VM。scope instance 不同的值不能未经聚合规则直接合并。

## 缺失与语义验证

- 关键 workload objective 缺失：候选不可评分。
- hard guardrail 缺失：候选不可发布。
- diagnostic 缺失：允许保留结果，但必须降低 evidence coverage。
- 同名字段不能自动映射；需抽查不同样本并用工具或实测验证语义。
- 缺失值不得自动填零，缺失指标不得触发静默权重重分配。

原始字段全集见 research/source-metric-inventory-2026-08-22.md；该盘点不是已批准 schema。
