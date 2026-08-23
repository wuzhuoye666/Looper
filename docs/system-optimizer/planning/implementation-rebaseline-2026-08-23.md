# System Optimizer 实际实现与原里程碑对照（2026-08-23）

> 状态：delivery baseline  
> 目的：冻结“当前实际能力”，防止模块增长被误报为里程碑完成。

## 计数口径

- 实现模块：统计 `packages/core/looper_core/system_opt/**/*.py`，排除两个
  `__init__.py`。当前为 **15 个实现模块**；“14 个”是加入
  `executor/runner.py` 前的快照。
- 修复前测试：8 个测试文件，**49 个 `def test_` 测试函数 / pytest 收集
  50 个 case**。差额来自一个 parametrized 测试函数展开为两个 case。
- 当前口径：10 个 System Optimizer 测试文件，**71 个 `def test_` 测试函数 / pytest
  收集 72 个 case**；仓库全量 pytest 收集 **293 个 case**。本轮 System Optimizer
  72/72 通过；独立 basetemp 排除已知 cloud confirmation token flaky 后 292/292 通过。
  未排除全量在该已知测试失败，不能写成 293/293，也不能沿用上一提交的 283/283。

模块数和测试数都不是里程碑完成标准；验收只按路线图的出口条件。

## 当前实际 vs 原 M1–M5

| 原里程碑 | 原计划出口 | 当前已经存在的实现切片 | 尚未完成/不能宣称 |
|---|---|---|---|
| M1 配置资产与安全底座 | 可安全采集、人工修改和恢复 | manifest/profile；显式根 raw inventory；环境指纹；动态域证据；simulated/local-linux/SSH backend；argv allowlist runner；preflight、snapshot、verify、rollback、租约；逐项来源/持久化/ownership 状态证据；显式 actor 授权；完整快照崩溃对账和 attention 恢复 CLI；阿里云 ECS 20 项重采为 18 succeeded/2 unavailable；真实修改/回滚、过期租约接管、故意 rollback failure 和 operator recovery 均完成 | 腾讯云 CVM 直接实测；跨 sysctl.d/tuned/发行版的最终优先级解析；真实进程 kill 而非过期租约构造的 crash 演练；已知 cloud confirmation flaky 使仓库不能记为 293/293 |
| M2 通用标准压力调优 | 受控环境输出 best observed | policy、metric contract、bootstrap improvement、Pareto、周期基线、共享预算和停止条件；synthetic general demo；阿里云 ECS 存储 5 候选真实多轮 | CPU/内存/NUMA/网络真实协议、跨组件组合复验、通用 Profile；本次无候选获得显著收益 |
| M3 workload 动态下钻 | 诊断路由后回到业务 workload 复验 | pressure/adverse-change/persistence/confidence 的组件内 Pareto 优先级、组件路由和 synthetic workload demo 被提前实现 | L0–L3 真实低开销采集器及开销 A/B；真实 workload 输入合同、业务复验和动态预算实测 |
| M4 平台集成 | API/权限/远程/UI 等候选 | Typer CLI；local-linux backend 已在 ECS 真实执行；存在 SSH backend 接口 | 本次不是 ssh-remote backend 验收；未接 HTTP API、事件投影、审批、UI、多节点协议 |
| M5 交付收尾 | 完整运行手册、实录、迁移和失败演练 | 文档树、公式溯源、synthetic/WSL2/阿里云单候选与多候选证据；本地和 ECS 283 测试全绿 | raw 到候选缺直接绑定；无完整 replay/验证器、failure drill、迁移说明、schema 稳定承诺和腾讯云 CVM 实录 |

## 定性结论

当前实现是“**M1 主体和两个真实配置项验证 + M2 存储真实多轮纵向切片 + M3 simulated
纵向切片 + M4 CLI/local-linux 切片 + M5 文档与实录切片**”，
不是 M1–M5 全部完成。提前实现双闭环有助于验证公式和接口能否串通，但不能改变
后续真实 Linux/CVM 验收门，也不能用 synthetic demo 证明真实优化收益。

后续每次验收都应更新本表的“尚未完成”列；不得因为文件、模块或测试数量增加而
静默修改原出口标准。
