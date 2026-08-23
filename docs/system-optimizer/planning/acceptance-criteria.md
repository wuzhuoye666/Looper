# System Optimizer 验收标准

> 状态：normative draft；具体数值型阈值待基线实测后确认。

## 文档门

- 当前入口不存在指向 legacy 的“当前合同”误导。
- 每份规范有状态、适用范围和 open decisions。
- 重要决定能追溯理由和历史变化。
- 文档不把论文、fixture、接口代码或推断写成本项目真实能力。
- 指标、评分、下钻和最终业务裁决的边界一致。

## 配置门

- 能全量报告发现、读取失败、权限不足、解析失败和不支持项。
- 当前、期望、生效、持久化和所有权状态不混淆。
- 人工修改和优化候选走同一安全执行链。
- 动态合法域经过目标能力验证。
- precondition、依赖、互斥和风险在 apply 前执行。
- inventory 内嵌环境指纹，并将枚举完整性与所有值可读性分开报告。
- observation-only 缺失项保持 null/unavailable，不为满足 schema 编造默认值。
- M1 文档候选按 `6 × 3 + IRQ + MTU = 20` 明确计数；CVM 验收另做目标实测。

## 工具与环境门

- 工具已安装、可执行和目标 capability 可用三者不混用。
- optional 工具缺失可降级并报告；run-specific critical 工具缺失必须 fail-closed。
- 安装计划与安装动作分离，未授权时不得静默修改 CVM 软件环境。
- WSL2 证据只验证代码路径，不得改变 CVM 的 unverified 状态。

## 安全门

- 任意 apply、verify、timeout、unknown 和 rollback failure 均有专项故障注入测试。
- 可能部分生效的当前项进入补偿范围。
- rollback 恢复实际 snapshot 并读回验证。
- unknown 或 rollback failure 后目标进入 needs-attention，后续写任务被阻止。
- 同一目标并发写被租约阻止；崩溃后先对账再恢复。

## 指标门

- 每个评分或优先级指标都有角色、方向、单位、作用域、阶段和来源。
- 每个采集器记录成本层级；“低开销”有目标环境 A/B 证据。
- 相同名称跨来源不自动合并。
- 上升、下降、目标和区间型指标分别正确处理。
- 近零、缺失、计数器重置和阶段变化不会静默产生虚假大比例。

## 通用调优门

- 标准压力声明主要组件和跨组件影响。
- 单组件结果与组合复验分开。
- 业务 workload 能力不由探针结果推断。
- Profile 带环境、基线、证据、风险和回滚状态。

## workload 调优门

- L0/L1 先于 L2/L3，昂贵采集有触发理由和限定窗口。
- 组件内二维优先级不进入整体业务得分。
- 允许保留多个竞争假设，相关性不冒充因果。
- 候选必须回到相同 workload 协议复验。
- 正确性、安全和 SLO 失败不能被其他收益补偿。
- 报告同时区分 frozen baseline、incumbent 和 general profile。

## 统计与结果门

- 原始值、重复次数、变异和覆盖率可追溯。
- 最小有效提升和停止参数有基线校准证据与用户确认。
- 关键目标或门禁缺失时不产生可比较总分。
- best observed、validated、portable 和 deployable 不混用。

## 测试与交付门

- 新测试使用本轮独立 basetemp，并记录并发条件。
- 单元、故障注入、simulated E2E 和全量回归分别报告。
- 真实 Linux/CVM 只有在对应实测通过后才改变 unverified 状态。
- 缓存过程优化不进入功能闭环验收，也不得被提前启用。
