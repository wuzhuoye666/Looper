# Looper System Optimizer 文档入口

> 状态：runnable vertical slice；CVM unverified  
> 日期：2026-08-24
> 当前阶段：M1/M2 阿里云受控切片完成；M3 动态纵向切片已接线，真实 CVM 验收仍开放。
> 实现口径：阿里云 ECS 已验证配置安全闭环、存储多轮和 CPU/Memory/Network-loopback
> 压力出数；腾讯云 CVM 与正向收益仍未验证。

## 当前结论

System Optimizer 是一个操作系统配置采集、人工管理和有限闭环调优系统。第一阶段只面向离线或受控环境，不是生产环境常驻自动控制器。

系统共享一套配置底座，但有两条不同的调优路径：

- 系统通用调优：用受控标准压力按组件搜索，产出通用基础 Profile。
- workload 场景调优：用业务目标评价结果，以低开销动态观测和按需微指标下钻决定先调查什么，产出场景 Profile。

组件内部的“当前不利压力 × 不利变化”二维优先级只负责下钻顺序，不进入整体业务评分。系统微指标负责路由、解释和门禁；最终场景候选由业务目标、SLO、安全和稳定性裁决。

未来可用缓存和中间结果复用加速优化器自身，但必须等功能闭环正确跑通后单独设计。当前不允许因历史相似结果跳过真实采集或真实测量。

## 文档地图

### 治理

- governance/rebaseline-source-2026-08-22.md：本轮重要讨论、纠偏和注意事项的完整来源记录。
- governance/decision-log.md：已确认、重新打开、待确认的架构决定。
- governance/terminology.md：通用调优、场景调优、微指标、优先级和评分等术语。
- governance/document-rules.md：规范状态、事实/推断、默认值和变更记录规则。
- governance/collaboration-protocol.md：独立 worktree、任务生命周期、统一集成、异常、测试隔离和用户决策边界。

### 架构

- architecture/overall.md：**总体架构 v2（权威）**——组件化分层（独立组件优化器 ×N + 总引擎调度/判断/打分 + 九层：压力器/采集器/组件优化器/回退器/负缓存/引擎），静态与动态两种运行情境、结束门禁、S0–S10 公式总线映射与实现状态、建议实现顺序。
- architecture/layer-specifications.md：**分层实现规范与目录说明**——目录↔层对应表、逐层验收门禁（含通过状态）、L4 guest 盲区契约、L7 负缓存红线、L8 三器官规范。
- architecture/configuration-plane.md：配置发现、采集、人工修改、Profile 和动态合法域。
- architecture/general-tuning.md：无业务载荷的标准压力调优。
- architecture/workload-tuning.md：workload 动态观测与有限闭环。
- architecture/drill-down-analysis.md：从业务症状到微指标、假设和干预验证。

### 契约

- contracts/metric-contract.md：指标方向、作用域、阶段、采集成本和计算输入。
- contracts/scoring-contract.md：组件内优先级、假设可信度、业务收益和门禁分离。
- contracts/formula-provenance.md：导师综合式、论文原式和项目扩展公式的来源、适用范围与禁止外推。

### 安全与执行

- safety/execution-and-recovery.md：安全事务、单写者、漂移、崩溃恢复和多节点边界。

### 规划与验收

- planning/roadmap.md：重新基线后的阶段计划、依赖和交付物。
- planning/acceptance-criteria.md：功能、证据、安全、指标和文档验收。
- planning/implementation-rebaseline-2026-08-23.md：当前实际能力与原 M1–M5 对照。
- planning/m1-state-ownership-recovery-contract-2026-08-23.md：M1 状态来源、逐项所有权授权、完整快照崩溃对账与未完成边界。
- planning/m2-component-pressure-contract-2026-08-23.md：五组件口径、标准阶段合同、校准与正式门禁的边界。
- planning/agent-work-ledger-2026-08-24.md：当前多 Agent 任务、依赖、交付、验收、合入和远端状态的唯一登记本。

### 调研与历史

- research/source-metric-inventory-2026-08-22.md：论文与当前套件的原始指标全量盘点，未筛选、未做语义合并。
- research/kernel-official-config-catalog-2026-08-23.md：按原验收口径核对的 20 个官方候选。
- research/wsl2-capability-probe-2026-08-23.md：WSL2 代码能力、缺接口与工具缺口实录。
- research/aliyun-ecs-m1-state-recovery-acceptance-2026-08-23.md：M1 20 项采集、状态归属、人工修改、崩溃对账与 rollback failure 恢复实测。
- research/aliyun-ecs-m2-component-calibration-2026-08-23.md：CPU/Memory/NUMA/Network 首次组件压力校准与不可外推边界。
- legacy/system-optimizer-m0-m1-2026-08-22.md：迁移前的 M0/M1 主方案，仅供追溯，不再是当前合同。

## 当前实施状态

| 范围 | 状态 | 说明 |
|---|---|---|
| 新架构与规范 | draft | 已确认核心方向，仍有明确 open decisions |
| 配置模型、inventory、Profile、安全执行 | Alibaba ECS KVM accepted | 腾讯云 CVM 仍须独立复验 |
| 通用标准压力闭环 | Alibaba ECS calibration in progress | 存储真实多轮；CPU/Memory/Network-loopback 首次出数；NUMA 单节点 unavailable；候选收益与组合复验未完成 |
| workload 动态下钻闭环 | simulated vertical slice | 真实低开销采集、下钻和业务复验未验证 |
| 真实 local Linux | Alibaba ECS partially verified | 不能外推腾讯云 CVM；仅按各实录声明的作用域使用 |
| 缓存与中间结果复用 | deferred | 功能闭环通过后进入过程优化阶段 |

## 进入 CVM 验证前的阻断项

继续改实现前至少需要完成：

1. 在 CVM 重采环境指纹、20 项接口/权限和工具缺口，不能复用 WSL 状态。
2. 为实际存在且可写的配置建立目标动态域、snapshot、verify 与 rollback 证据。
3. 选定组件压力/workload 后，将对应工具提升为 run-specific critical 并完成 preflight。
4. 对 L0/L1 “低开销”采集做 CVM A/B 开销验证。
5. 用相同压力协议完成重复性、区分度、失败演练和回滚验证。

具体阈值、重复次数、置信水平和无改进轮次尚未通过基线实测校准，不得写成隐式默认值。

## 阅读规则

- 本入口和各规范文件代表当前设计；legacy 只代表历史。
- confirmed 表示用户已明确确认；draft 表示可讨论方案；open 表示不得写入实现的未决项。
- 论文数字、synthetic fixture 和设计推断不得作为本项目实测结果。
- 同名指标不默认等价，任何筛选、映射、阈值和缺失处理都必须记录依据。
