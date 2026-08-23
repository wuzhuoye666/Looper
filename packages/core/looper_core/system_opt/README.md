# System Optimizer 模块

> 状态：runnable vertical slice / CVM unverified  
> 当前事实：M1 主体与 M2/M3 simulated 纵向切片已经跑通；WSL2 完成只读 Linux
> inventory 验证。真实 CVM 写入、压力区分度与收益仍未验证。  
> 当前合同入口：../../../../docs/system-optimizer/README.md

## 模块定位

本模块承载操作系统 guest 配置的纯逻辑与安全执行接口。新的产品总线先建立配置采集、人工修改、验证、回滚和审计底座，再分别支持：

- 无业务载荷的标准压力通用调优。
- workload 业务目标驱动、低开销观测和微指标下钻的场景调优。

第一阶段是离线或受控环境中的有限任务，不是 always-on 自动调参 daemon。

## 当前已有代码

| 文件 | 当前能力 | 状态边界 |
|---|---|---|
| config_manifest.py | 配置项、null observation、值域、风险、依赖声明 | CVM 合法域仍需目标实测 |
| inventory.py | 环境指纹、manifest/raw/tool inventory、完整性与权限状态 | WSL2 只读已测，CVM 未测 |
| domain.py | 声明域、目标能力域与任务授权域求交 | 当前目标证据必须外部提供 |
| profiles.py | Profile 展开、条件与参数映射 | 人工意图长期持久化仍待集成 |
| safety.py | preflight、snapshot、apply、verify、rollback 与补偿 | simulated 故障注入通过，真实写未测 |
| lease.py | 单目标文件租约、fencing 和 needs-attention | 多节点协调不在当前范围 |
| policy/scoring/tuning.py | 公式、门禁、诊断优先级、搜索与停止条件 | 真实压力/workload 未测 |
| measurement.py | argv measurement adapter | 工具能力必须逐目标 preflight |
| demo.py | general/workload deterministic synthetic E2E | 只证明代码闭环，不证明性能收益 |
| executor/simulated.py | 确定性模拟目标与故障注入基础 | 是当前唯一可安全作为默认测试基础的 backend |
| executor/local_linux.py | Linux argv、preflight、读回及权限状态 | WSL2 只读已测；写入默认禁用 |
| executor/ssh_remote.py | 远程执行接口基础 | 真实 CVM 未验证，默认禁用 |

## 已修复的 A 级问题

1. SafetyController 已执行 backend preflight，preconditions 不再只声明不执行。
2. 当前 apply 项在执行前即进入补偿集合；failed、timeout 或 unknown 不再逃逸回滚。

两项已有专项测试；这只关闭代码级 A 级缺口，不替代 CVM 真实写入与故障恢复演练。

## 新架构对实现的约束

- 配置采集是第一等功能，不能只在优化候选 apply 时读取配置。
- 用户手动修改与调优候选必须走同一安全执行链。
- 静态 ValueDomain 不能替代目标能力发现和任务授权域。
- 单一 category 不能充分表达 primary component 与 related components。
- workload 的系统微指标只负责诊断、解释和门禁，不进入整体业务得分。
- 组件内部二维优先级不能变成全局候选公式。
- 指标方向支持 maximize、minimize、target、range 和 diagnostic-only，不能统一把上升当成退化。
- 未来缓存不得在当前阶段替代真实采集和真实复测。

## 安全原则

系统修改遵循：preflight → snapshot → apply → verify → measure → rollback-or-explicit-keep。

- 多接口修改是补偿事务，不宣称内核级原子提交。
- snapshot 保存实际旧值，rollback 后再次读回验证。
- unknown 或 rollback failure 进入 needs-attention，并停止后续写入。
- 真实 backend 需要显式能力、权限和操作者授权。
- 同一目标并发写和崩溃恢复协议在确认前不得自行设置默认行为。

## 开发顺序

1. 以 docs/system-optimizer 下的新规范完成架构评审。
2. 修复已知 A 级安全问题。
3. 完成配置资产、人工修改、动态合法域和单目标恢复。
4. 再实现通用标准压力闭环。
5. 之后实现 workload 低开销观测、微指标下钻和业务复验。
6. 功能闭环验收后才进入缓存和中间结果复用。

历史 M0 模块 README 已无损保存于 docs/system-optimizer/legacy/module-readme-m0-2026-08-22.md。
