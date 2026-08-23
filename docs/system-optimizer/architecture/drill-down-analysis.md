# 下钻分析

> 状态：draft；方向和边界 confirmed，具体路由算法 open。

## 目的

下钻分析用有限采集成本把业务症状逐步缩小为可验证的配置假设。它不是直接从单个高指标映射到参数，也不是最终业务评分器。

## 分层

| 层 | 问题 | 证据示例 |
|---|---|---|
| 业务症状 | workload 是否退化、违反 SLO 或未达目标 | 吞吐、P99、完成时间、错误率 |
| 资源域 | 哪些资源域与症状同时存在压力 | CPU queue、PSI、IO latency、retransmit |
| 组件内部 | 组件内部哪个机制更可疑 | 单核饱和、NUMA remote、cache miss、queue depth |
| 假设 | 哪条机制可能解释业务变化 | 带来源、时间关系和竞争假设的陈述 |
| 控制 | 哪个安全配置可以验证假设 | 目标实际支持且在任务授权域内的参数 |
| 干预复验 | 修改是否使业务按预期变化 | 可重复 A/B、业务指标和副作用 |

## 组件内二维优先级

优先级输入至少包括方向感知的当前压力、不利变化、持续性、workload 阶段、作用域和不确定性。二维高/低是调查分层，不是固定全局分数。

必须防止：

- 基线接近零导致相对比例爆炸。
- 瞬时尖峰或阶段切换冒充持续瓶颈。
- 整机平均掩盖核、NUMA、设备或队列热点。
- 指标上升方向被统一解释。
- PMU multiplexing、采样丢失或 profiler 开销被忽略。

## 多假设

诊断结果 SHOULD 同时保存 primary component、related components、证据支持和竞争假设。低成本探测可以用来淘汰假设，但在干预复验前不得标记 confirmed root cause。

## 验证语言

- observed association：只观察到共同变化。
- supported hypothesis：重复观测和时间关系支持，但未干预。
- intervention-supported：受控修改后业务结果按预期可重复变化。
- unresolved：证据冲突、缺失或无法安全干预。

第一阶段不得使用“自动找到根因”描述仅达到 observed association 的结果。
