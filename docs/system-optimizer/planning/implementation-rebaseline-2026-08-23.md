# System Optimizer 实际实现与原里程碑对照（2026-08-23）

> 状态：delivery baseline  
> 目的：冻结“当前实际能力”，防止模块增长被误报为里程碑完成。

## 计数口径

- 实现模块：统计 `packages/core/looper_core/system_opt/**/*.py`，排除两个
  `__init__.py`。当前为 **15 个实现模块**；“14 个”是加入
  `executor/runner.py` 前的快照。
- 修复前测试：8 个测试文件，**49 个 `def test_` 测试函数 / pytest 收集
  50 个 case**。差额来自一个 parametrized 测试函数展开为两个 case。
- 本轮最终口径：8 个测试文件，**55 个测试函数 / 56 个 pytest case**。相对修复前
  新增 6 个函数，覆盖 raw 完整性、null observation、权限拒绝、缺工具、工具清单
  和精确 20 项类别计数。
- 仓库全量：pytest 收集 277 个 case，使用仓库外独立 basetemp 全部通过。

模块数和测试数都不是里程碑完成标准；验收只按路线图的出口条件。

## 当前实际 vs 原 M1–M5

| 原里程碑 | 原计划出口 | 当前已经存在的实现切片 | 尚未完成/不能宣称 |
|---|---|---|---|
| M1 配置资产与安全底座 | 可安全采集、人工修改和恢复 | manifest/profile；显式根 raw inventory；环境指纹；动态域证据；simulated/local-linux/SSH backend；argv allowlist runner；preflight、snapshot、verify、rollback、租约；CLI validate/inventory/raw/manual | CVM 直接实测；系统所有配置的无限制“全量”；持久化与 ownership 自动发现；真实写入和崩溃恢复演练 |
| M2 通用标准压力调优 | 受控环境输出 best observed | policy、metric contract、bootstrap improvement、Pareto、停止条件、候选引擎和 synthetic general demo 被提前实现 | CPU/内存/存储/网络真实压力协议、清理、稳定性与区分度；真实 Linux best observed |
| M3 workload 动态下钻 | 诊断路由后回到业务 workload 复验 | pressure/adverse-change/persistence/confidence 的组件内 Pareto 优先级、组件路由和 synthetic workload demo 被提前实现 | L0–L3 真实低开销采集器及开销 A/B；真实 workload 输入合同、业务复验和动态预算实测 |
| M4 平台集成 | API/权限/远程/UI 等候选 | 只提前接入现有 Typer CLI；存在 SSH backend 接口 | 未接 HTTP API、事件投影、审批、UI、多节点协议；SSH 未做目标四层验证 |
| M5 交付收尾 | 完整运行手册、实录、迁移和失败演练 | 文档树、公式溯源、synthetic artifact、WSL2 只读采集证据和已知限制已有切片 | 尚未达到完整可交付版本；CVM 实录、迁移说明、完整 failure drill、schema 稳定承诺未完成 |

## 定性结论

当前实现是“**M1 主体 + M2/M3 的 simulated 纵向切片 + M4 CLI 切片 + M5 文档切片**”，
不是 M1–M5 全部完成。提前实现双闭环有助于验证公式和接口能否串通，但不能改变
后续真实 Linux/CVM 验收门，也不能用 synthetic demo 证明真实优化收益。

后续每次验收都应更新本表的“尚未完成”列；不得因为文件、模块或测试数量增加而
静默修改原出口标准。
