# System Optimizer 术语表

> 状态：current draft；已确认术语标注为 confirmed。

| 术语 | 定义 | 状态 |
|---|---|---|
| 配置平面 | 发现、采集、表达、人工修改、版本化、施加、验证和回滚 OS 配置的共同底座 | confirmed |
| 通用调优 | 不使用真实业务 workload，以受控标准压力按组件搜索通用基础配置 | confirmed |
| 标准压力 | 协议固定、可重复、可归属到组件的测试压力；不是生产 workload，也不是字面空载 | confirmed |
| workload 场景调优 | 以业务 workload 定义目标，在有限任务中动态观测、下钻、干预和复验 | confirmed |
| 宏观指标 | 低开销业务结果或资源压力指标，用于判断是否退化和路由候选组件 | confirmed |
| 微指标 | 组件内部更细粒度的计数器、事件或 trace 字段，由下钻按需启用 | confirmed |
| 当前不利压力 | 当前值相对容量、SLO、安全界限或同阶段正常范围的压力，而非简单数值高 | confirmed |
| 不利变化 | 按指标语义方向计算的退化变化；可能表现为上升、下降、偏离目标或越界 | confirmed |
| 组件内二维优先级 | 当前不利压力与不利变化组成的组件内部调查优先级 | confirmed |
| 诊断路由 | 从业务症状和宏观压力选择一个或多个候选组件的过程，不等同于最终评分 | draft |
| 瓶颈假设 | 基于观测关系提出、尚需干预实验验证的原因解释 | confirmed |
| 业务目标 | workload manifest 声明的吞吐、延迟、完成时间、成本或其他最终目标 | confirmed |
| guardrail | 不允许被其他收益补偿的正确性、安全、SLO 或稳定性约束 | confirmed |
| frozen baseline | 本次任务冻结的原始配置和可比测量，供最终报告使用 | confirmed |
| incumbent | 当前证据下的最佳可行候选，供搜索替换决策使用 | confirmed |
| best observed | 本次环境、协议和预算内观测到的最好候选，不代表全局最优 | confirmed |
| Profile | 带环境适用条件、配置值、证据、风险和回滚信息的配置集合 | draft |
| workload cache state | 测量协议中的冷缓存、热缓存或重置状态 | confirmed distinction |
| optimizer evidence cache | 未来复用历史采集、测量或候选结果的过程优化能力 | deferred |

禁止用“局部调优”同时指组件内部下钻和 workload 场景调优。需要明确写“组件内调优”或“场景调优”。
