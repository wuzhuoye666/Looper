# 并行工作包规划（2026-08-23）

> 状态：planning；用于把剩余实现拆给多个 agent 并行执行。
> 每个包的 agent 必读：`architecture/overall.md`（架构 v2 权威）、
> `architecture/layer-specifications.md`（分层规范与门禁）、
> `contracts/formula-provenance.md`（公式总线，动代码前先查）。
> 通用纪律：每包独立分支/worktree；测试驱动；fail-closed；不引入未经用户
> 确认的阈值/权重（发现 open decision → 出提案停下等确认，不得自行定值）；
> 提交风格 `feat(system-opt)/fix(system-opt)/docs(system-opt): ...`；只改本包
> 文件清单内的文件，跨包接口变更先在本文件登记。

## 依赖与并行视图

```
可立即并行（互不依赖）：
  PKG-A L5 组件优化器改造     PKG-B L3 压/采解耦      PKG-C S8/S9 结果向量
                                                      PKG-E 真实环境验证（服务器）
串行链：
  PKG-A ──> PKG-D 引擎主循环 ──> PKG-F CLI与demo
文档线（随时可做，低优先）：
  PKG-G 动态相位设计草案
```

已完成的层（勿重做）：L0–L2、L4 采集器（collector.py）、L6 回退器
（rollback.py，退化级=S8 占位）、L7 负缓存（negative_cache.py）、L8 三器官
（engine/scorer|judge|scheduler，未整合主循环）。

---

## PKG-A：L5 组件优化器改造（终裁上收）

- 范围：`tuning.py`、新 `component.py`（或等价）、`policy.py` 只读参考。
- 内容：
  1. 组件优化器包装类：一个组件 = manifest+policy+protocol+公式映射钩子的
     实例；对外接口＝`report() -> 候选评估列表（CandidateEvaluation）+ 组件
     打分输入`，不再自己出"最终接受"结论；
  2. `tuning.py` 现有 `accepted` 字段语义改为"组件内晋级建议"，终裁走
     L8 `judge.evaluate_candidate`（保留旧字段名以兼容存量工件，只改语义
     并在文档标注）；
  3. 公式映射钩子：输入 L4 快照+测量批次，输出候选建议（具体值+优先级）；
     第一版允许返回空（搜索兜底），但接口必须存在并被测试。
- 验收门禁：组件优化器单测（上报结构、不再终裁）；改造后现有组件闭环测试
  全绿；引擎 judge 与组件报告的字段对接测试。
- 红线：不动 engine/ 内文件（接口对接处由 PKG-D 消费）。

## PKG-B：L3 压/采解耦 + 公式映射候选生成

- 范围：`pressure.py`、`examples/system-optimizer/*_pressure_measure.py`、
  `collector.py` 只读消费。
- 内容：
  1. 把现测量脚本拆成"压力构建"与"指标采集"两段：压力脚本只负责加载与
     阶段协议，采集走 L4 collector（同负载可换采集器）；
  2. 压力协议 schema 增加声明"本协议采集哪些指标、由谁采"（只增不删，
     v1alpha1 兼容）；
  3. ⚠️ 含 open decision：`PressureTransform`/`AdverseChangeTransform` 的
     具体公式（F-PROJECT-002 标注未确认）。**必须先产出 2–3 个候选公式提案
     （含依据）交用户确认，确认前不得把任何变换写进代码。**
- 验收门禁：解耦后 CPU/内存/存储三协议在 simulated/本地测试等价出数；
  采集开销可单独开关并有测试；提案文档落 `contracts/` 评审节。

## PKG-C：S8 结果向量 + S9 组合复验（纯逻辑，无环境依赖）

- 范围：新 `result_vector.py`（或并入 scoring.py，开工前在本文件登记选择）。
- 内容：
  1. `U_general(x) = (U_cpu, U_memory, U_storage, U_network, U_stability,
     U_regression)` 六元向量模型（PROJECT-DRAFT F-PROJECT-004）；
  2. 向量 Pareto 层计算与词典序决胜（复用现有 tie_break 思想）；
  3. S9：`best-observed → validated` 晋升合同（复验次数/跨时间/跨环境为
     任务输入，不内置默认）；
  4. 打通 L6 退化级回退的触发字段（`rollback.py` 的 REGRESSION 占位解除
     条件；执行体可以仍等 PKG-D，但触发判定先就位）。
- ⚠️ open decision：各 U_i 的归一化与量纲合并方式未定义——同 PKG-B 规则，
  提案制。
- 验收门禁：向量/Pareto/晋升纯逻辑全测；与 rollback 记录的对接测试。

## PKG-D：L8 引擎主循环整合（依赖 PKG-A）

- 范围：`engine/`（新增 loop.py）、消费 PKG-A 上报接口、调用 L7/L6。
- 内容：实现架构 §4 主循环——打分→调度（查负缓存）→组件出候选→判断器
  （S0/S2/S7）→施加+测量（经 L1/L3）→无改善写负缓存+回退→S10 停止判定
  （含相位级结束门禁：停止时验证系统=基线，用 L6 `verify_phase_restoration`）。
  所有循环参数（预算/K 轮/N 窗）为任务输入。
- 验收门禁：simulated 后端全循环冒烟（≥2 组件、含缓存命中跳过、含一次门禁
  拒绝回退、含相位结束回基线验证）；全仓测试绿。

## PKG-E：真实环境验证（服务器，与代码线并行）

- 范围：`examples/system-optimizer/aliyun-ecs-network-*`、`.artifacts/`、
  研究实录文档；不改 packages 代码（发现代码问题→报回对应包 owner）。
- 内容：
  1. 网络组件真实闭环：3 号机（8.148.249.35 已失联；备选 8.148.238.132）
     作受控 peer，装 iperf3，跑 校准→派生门→CC 候选闭环（cubic 基线，
     bbr/reno 候选，授权域走用户确认）；
  2. Guest 盲区实测：collector 在 1 号机与 3 号机各采一轮
     cpu/memory/network/storage/numa 快照，PMU/NUMA 不可读证据落 artifacts；
  3. 会话收尾：恢复 1 号机 tuned 并验证 governor 回 performance（除非用户
     另有指示）。
- 验收门禁：每闭环按 M2 合同（report-only 校准→gate 派生→hard-gate 闭环）
  全链证据；实录文档含能证明/不能证明两节；凭证不落盘。

## PKG-F：引擎 CLI 与 demo（依赖 PKG-D）

- 范围：`services/api/looper_api/cli.py`、demo 素材。
- 内容：`system-opt engine-run`（多组件编排入口，接 PKG-D 循环）、
  `system-opt cache inspect`（负缓存查询）、Windows 可跑的 simulated 全流程
  demo 三条命令内完成；README 快速开始更新。
- 验收门禁：demo 在 Windows 开发机可复现；CLI 测试补齐。

## PKG-G：动态相位设计草案（文档线，低优先）

- 范围：`docs/system-optimizer/architecture/workload-tuning.md` 扩写。
- 内容：S3 真实组件路由（症状→多组件假设）、L0–L3 观测分层与开销 A/B、
  结束门禁参数化合同、重激活判据提案。只出设计，不写实现。

---

## 登记本

| 包 | 状态 | 负责 | 备注 |
|---|---|---|---|
| PKG-A | ✅ 完成 2026-08-23 | 主 agent（zcode） | component.py + 语义标注 + 6 测试；全仓 167 绿 |
| PKG-B | 待领 | — | |
| PKG-C | 待领 | — | |
| PKG-D | 进行中 | 主 agent（zcode） | 依赖 PKG-A 已满足，开工 |
| PKG-E | 待领 | — | 服务器凭证由用户提供，不落盘 |
| PKG-F | 待领（等 PKG-D） | — | |
| PKG-G | 待领（低优先） | — | |

## 冲突与协调

| 文件 | 涉及包 | 协调 |
|---|---|---|
| tuning.py / policy.py | A（改）、D（读） | D 在 A 合入后开工 |
| pressure.py / examples 脚本 | B 独占 | — |
| scoring.py | C 可扩展、B 只读 | C 若并入 scoring.py 先登记 |
| engine/ | D 独占（A 不碰） | — |
| cli.py | F 独占 | — |
| examples/artifacts | E 独占 | — |

包间接口变更：改本文件"登记"节 + 决策日志，不私聊约定。
