# System Optimizer 总体架构

> 状态：architecture draft；核心边界 confirmed，模块接口待确认。

## 产品总线

共同底座：

配置发现与全量采集 → 状态与所有权建模 → 用户查看/手动修改 → 快照 → 安全施加 → 读回验证 → 回滚/显式保留 → 审计

其上有两条不同闭环：

- 通用闭环：标准压力 → 组件搜索 → 跨组件复验 → 通用 Profile。
- 场景闭环：业务观测 → 组件路由 → 微指标下钻 → 假设干预 → 业务复验 → 场景 Profile。

## 逻辑组件

| 组件 | 职责 | 不负责 |
|---|---|---|
| Configuration Inventory | 发现、读取、验证并版本化配置事实 | 不自行决定哪些值更优 |
| Manual Configuration | 表达用户修改意图 | 不绕过安全执行器 |
| Capability/Domain Resolver | 发现目标真实能力并收窄任务范围 | 不凭静态枚举假装目标支持 |
| Safety Executor | preflight、snapshot、apply、verify、rollback | 不评价业务收益 |
| General Tuner | 运行标准压力并形成组件/通用 Profile | 不冒充真实 workload 能力 |
| Workload Observer | 同步采集业务和低开销系统指标 | 不把观测关联写成根因 |
| Drill-down Engine | 选择组件、触发微指标、形成假设 | 不决定最终业务评分 |
| Candidate Evaluator | 执行门禁、业务收益和稳定性判断 | 不让 soft 收益补偿 hard failure |
| Evidence Store | 保存原始事实、派生结果和决策版本 | 不复用历史结果替代当前测量 |

## 有限任务生命周期

建议状态：created → preflight → baseline → observing/probing → diagnosing → applying → verifying → measuring → deciding → rollback-or-keep → completed。

任一阶段可以进入 rejected、failed、cancelled 或 needs-attention。只有完成对账和回滚验证的任务才能宣称目标已恢复。

配置资产的读取和历史保存不依赖优化任务持续运行。优化任务结束后不得继续自动改参。

## 数据流原则

- 原始采集事实不可被派生分数覆盖。
- 每个派生值引用输入指标、公式版本和比较基准。
- 每个候选引用配置快照、workload、阶段、环境和工具身份。
- 通用 Profile 与场景 Profile 明确区分，不默认跨环境可移植。
- 所有最终结论可回到原始证据重算。

## 当前不做

- always-on 在线自治控制。
- 按 workload 阶段实时切换 Profile。
- 修改 hypervisor/宿主机、应用代码、编译器或 kernel patch。
- 未授权购买、启动或销毁云资源。
- 用 RL/LLM 绕过候选、安全门禁或证据规则。
- 在闭环正确性完成前实现证据缓存和结果复用。
