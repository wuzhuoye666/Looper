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
- ⚠️ 状态修正（2026-08-23 A 级审计）：当前 StandardPressureProtocol 覆盖
  CPU/内存/网络(loopback)；**存储仅有 fio_randread_measure.py，尚未纳入协议**
  ——存储协议纳入是本包子项。且本包与 L4 重构强耦合：解耦后采集必须走
  L4，L4 现仅支持 /proc、/sys 微指标、无法采压力工具主指标，**本包的解耦
  部分阻塞，等 L4 新采集合同**（L4 修复由 GPT agent 承担，见登记本）。
- 验收门禁（修正后）：解耦后 CPU/内存/网络三协议在 simulated/本地测试
  等价出数；存储协议补齐并同等验收；采集开销可单独开关并有测试；
  提案文档落 `contracts/` 评审节。

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

## PKG-H：外部只读审查整改（DeepSeek agent，2026-08-23 起）

> 来源：`Looper-system-optimizer-只读审查报告-20260823.md`（20 项声明经主 agent
> 逐条对照源码核实，18 项属实、2 项框架性偏差，分诊见下）。
> 治理：独立 worktree/分支；无推送权（主 agent 统一推）；只改本包清单内文件；
> 公式/阈值类改动必须先过 formula-provenance.md 登记并经用户批准。

- **第一批（已派发，2026-08-23 用户批准）**：
  - M7：补登记 3 个未登记 formula_id（`F-PROJECT-S6-S7/v1alpha1`、
    `F-PROJECT-PRESSURE-CV/v1alpha1`、`F-PROJECT-CONDITION-BOOTSTRAP/v1`）
    ——**只改 formula-provenance.md 文本，不改 pressure/mapping 代码**，登记内容用户过目；
  - M12：`scoring.py` assert 输入校验改显式 raise；
  - C6：改善量方向契约显式化（helper + 断言，消除 :219 硬编码全 MAXIMIZE 的隐性契约）；
  - C7：三套 Pareto 收敛到 `analysis.pareto_ranks` + 黄金值测试证明数值不变
    （`scoring._priority_dominates` / `result_vector._dominates` 改薄封装）；
  - §4 杂项：`tuning._search_space` 的 related_components 漏项、
    `scoring.py` `10**9` 哨兵改 `math.inf`。
- 文件清单：`scoring.py`、`analysis.py`（根 packages/core/looper_core/）、
  `tuning.py`、`docs/system-optimizer/contracts/formula-provenance.md`、相关 tests。
- 红线：**不得触碰** `collector.py`、`pressure/`、`examples/system-optimizer/*pressure*`
  （GPT PKG-B 独占）；不得动 `engine/`（主 agent）；数值口径变更（M5/M4 统一分位数/CV）
  会改变既有证据 digest，须随公式版本升级，未批前不动。

## 审查问题分诊总表（2026-08-23，未处理项跟踪）

| 项 | 一句话 | 状态 / 归属 | 阻塞点 |
|---|---|---|---|
| C1 | L5/L8 双写裁决、终裁上收未完成 | 主 agent（PKG-A 后续阶段，已登记 layer-spec §1） | 无，排期问题；特征测试先行 |
| C2 | 压/采解耦（老 adapter 直解 MeasurementBatch） | **GPT PKG-B**（SO-D016） | 等 GPT 新 L4 合同 |
| C3 | 预筛跨组件混比 | ✅ 已修（SO-D018，2026-08-23）：tracker 按组件隔离 + 回归测试 | 无；"轮级整体效用"聚合仍 open |
| C4 | 路由逻辑三处散落 | 主 agent，随 C1 重构一并收敛 | 依附 C1 |
| C5 | STABILITY_REJECTED 无生产者 | 待用户决策：稳定性拒绝是否入 L7 + 证据 digest 绑定语义 | 决策日志登记后实施小 |
| C6 | 方向双重编码 | **DeepSeek 第一批** | — |
| C7 | 三套 Pareto | **DeepSeek 第一批** | — |
| M1 | adverse_change 量纲混比 | 待用户拍板 S4 修订方向（除 scale 归一 vs 绝对距离另立字段）→ 可派 DeepSeek 实施 | 公式修订需批准 |
| M2/M3 | 缺 scale 静默兜底 | 待破坏面扫描（examples/.artifacts 缺 scale 合同计数）→ 用户确认 fail-closed 时机 | 兼容性影响 |
| M4 | CV 口径 ddof 不一（修正：pressure/mapping 实为一致 ddof=1，仅 analysis.summarize pstdev 不同） | 第二批，随公式版本 /v2 统一 | 数值变更需版本化 |
| M5 | 分位数两套定义（同名 LCB95 出不同数） | 第二批，统一到 analysis.quantile 插值 + F-PROJECT-CONDITION-BOOTSTRAP 升 /v2 | 数值变更需版本化 |
| M6 | LCB 实为双侧 CI 下界 | 待用户定语义（单侧/双侧写死进登记表） | 登记表措辞 |
| M7 | 3 个 formula_id 未登记 | **DeepSeek 第一批** | — |
| M8 | 重采样 2000 硬编码 + minimum_samples 默认 1 | 第二批：参数化 + 公式 /v2 | 数值变更需版本化 |
| M9 | confidence 实为样本充足率 | 缓：证据 schema 字段更名需迁移策略 | 用户定迁移 |
| M10 | UTILIZATION 负值静默 clamp | 第二批：负值显式 unavailable + 调用方适配 | 小 |
| M11 | S9 复验观测无真实生产者（passed 恒真） | 主 agent，随 PKG-G 动态相设计（复验测量路径） | 设计依赖 |
| M12 | assert 输入校验 | **DeepSeek 第一批** | — |
| M13 | 固定 seed 模式泄漏 | 缓：改 seed 派生会改全部可复现 digest，爆炸半径大 | 等 C1 后版本化 |
| §4.2 | 每次调用重建 generator | 并入 M13 处理 | 同上 |

---

## 登记本

| 包 | 状态 | 负责 | 备注 |
|---|---|---|---|
| PKG-A | ✅ 完成 2026-08-23 | 主 agent（zcode） | component.py + 语义标注 + 6 测试；全仓 167 绿 |
| PKG-B | 🟡 部分阻塞 | 待领 | 解耦子项等 L4 新合同（L4 修复：GPT agent 进行中）；存储协议子项不依赖 L4 可先做 |
| PKG-C | ✅ 完成 2026-08-23 | 主 agent（zcode） | result_vector.py（S8 六元向量+Pareto+任务决胜、S9 晋升合同 fail-closed、L6c 触发判定）+ 11 测试；归一化保持任务注入式提案待确认 |
| PKG-D | ✅ 完成 2026-08-23 | 主 agent（zcode） | engine/loop.py 主循环（调度→组件执行→终裁→负缓存→S10 停止→相位门禁）+ 6 测试 |
| PKG-E | 🟡 大部分完成 2026-08-23 | 主 agent | 盲区双机实测✅ + tuned 恢复✅ + 网络会话资产就绪✅；真实 peer 闭环✅（用户点出改走 VPC 内网后完成：bbr/reno 均未达显著，全部回滚；公网路径不适合作吞吐通道）|
| PKG-F | ✅ 完成 2026-08-23 | 主 agent（zcode） | engine-demo/cache-inspect 命令 + 4 测试；Windows 全流程 demo 一条命令可复现 |
| PKG-G | 待领（低优先） | — | |
| PKG-H | 🟡 第一批进行中 2026-08-23 | DeepSeek agent | M7/M12/C6/C7/§4 杂项；治理见 PKG-H 节；第二批（M1-M8/M10 等）按分诊表逐步解锁 |

## 登记补充（2026-08-23 A 级审计）

- 🔴 已确认冲突：L4 collector.py 仅采集 /proc、/sys 微指标，架构要求 L4
  同时采集主指标+分布+微指标（含压力工具输出解析）。修复责任：GPT agent。
  PKG-B 解耦子项、PKG-A 公式映射钩子的实际供数均等待新 L4 合同。
- 主 agent 不接触其他工作区的未提交修改。
- 🟡 **误放事故（2026-08-23 晚，用户确认非人工搬运）**：GPT agent 执行 PKG-B
  期间把工作目录误配到主 agent worktree（`Looper-system-optimizer/`），
  多波写入未提交文件（collector.py / pressure/__init__.py 修改 +
  4 个 collection 测试文件，18:56-19:55+）。其指派 worktree（`Looper-l4-fix/`）
  干净，**误放文件是其 PKG-B 工作唯一副本，暂停后需字节级保全搬迁**。
  处置规则见工作区 `AGENTS.md` §十四。主 agent 当晚回归已用 `--ignore`
  隔离不完整文件集，未吸收、未删除、未提交任何外来文件。

## 冲突与协调

| 文件 | 涉及包 | 协调 |
|---|---|---|
| tuning.py / policy.py | A（改）、D（读）、H（§4 杂项改） | H 的改动限于 _search_space related_components，不碰 A 已完成的组件包装 |
| pressure.py / examples 脚本 | B 独占（H 不得触碰） | — |
| scoring.py | C 可扩展、B 只读、H 第一批（M12/C6/C7/哨兵） | H 改动须带黄金值测试，数值不变 |
| analysis.py（looper_core 根） | H 第一批（C7 收敛） | 黄金值测试证明 pareto_ranks 数值不变；analysis 为全仓共享模块，回归全绿才可合 |
| engine/ | D 独占（A/H 不碰） | — |
| cli.py | F 独占 | — |
| examples/artifacts | E 独占 | — |
| formula-provenance.md | H 第一批（M7 补登记） | 登记条目用户过目后合入；数值口径修订（M1/M4/M5/M8）另批 |

包间接口变更：改本文件"登记"节 + 决策日志，不私聊约定。
