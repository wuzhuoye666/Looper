# workload 场景调优

> 状态：architecture draft，**方向已确认**（SO-D019，用户 2026-08-23）：O0-O3
> 定名、D2 两条硬规则、D5 重激活 A+B 组合；L7 第二条目类型细节与全部数值
> 校准仍 open（提案制）。有限闭环、低开销优先和业务结果裁决已 confirmed。
> 本文所有数值参数均为**占位符（待校准）**，不构成实现默认值。

## 术语对齐：观察分层 O0–O3（消歧）

本文原先用 "L0 业务指标 / L1 低开销系统指标 / L2 微指标 / L3 trace" 描述观察
深度，与九层架构（overall.md 的 L0 执行后端 … L8 引擎）撞名。自本次扩写起统一
改称**观察分层 O0–O3**，与九层架构的 L 编号互不混用：

| 观察层 | 内容 | 采集载体 | 开销 | 授权 |
|---|---|---|---|---|
| O0 | 业务指标（吞吐/SLO 达成/尾延迟/正确性） | workload 合同自带输出 | 近零（读任务自身产物） | 任务合同 |
| O1 | 低开销系统粗指标（CPU busy、PSI、netdev、iostat 级计数器） | L4 采集器 builtin 集，固定节拍 | 有界、可 A/B | 任务合同 |
| O2 | 组件微指标（PMU、slab、TCP ext、per-CPU 细账） | L4 采集器按组件触发窗口 | 显著、须证据 | 路由决策 + 任务合同 |
| O3 | 短时 trace（perf trace / eBPF / 抓包） | 授权时间盒内一次性 | 最高、时间盒限定 | 显式单独授权 |

升级规则：O1 常态；O2 只在被路由选中的组件上开窗；O3 只在 O2 证据不足且
显式授权时短时启用。**禁止全量常开 O2/O3**（SO-D007）。

## 目标

利用 workload 表达真实场景，在受控有限任务中发现动态瓶颈、验证配置干预，并形成只对声明环境和场景成立的场景 Profile。

## 主循环

1. workload 合同声明业务目标、SLO、正确性、阶段和输入身份。
2. 以原始配置或经确认的通用 Profile 建立冻结基线。
3. 运行 workload，同步采集 O0 业务指标和 O1 低开销系统指标。
4. 识别业务退化、资源压力和可比 workload 阶段。
5. 通过诊断路由保留一个或多个候选组件。
6. 对组件按需启用 O2 微指标；必要且授权时短时启用 O3 trace。
7. 在组件内部计算当前不利压力与不利变化，形成有证据的瓶颈假设。
8. 将假设映射到安全可控配置，执行单轮干预。
9. 用相同 workload 协议复测；先过正确性、安全和 SLO，再判断业务收益。
10. 回滚或显式保留，重新观测并重新分配预算，直到有限停止条件触发。

## 核心分离

- 系统微指标决定“看哪里、为什么可能有问题”。
- 业务指标决定“改动是否成功”。
- 采集成本和证据充分性决定“是否值得继续下钻”。
- 安全、正确性和 SLO 决定“是否允许候选晋级”。

## 阶段与基线

初始化、加载、稳态、checkpoint 和清理不能混为同一分布。第一阶段允许 manifest 提供显式阶段；自动阶段识别是 open decision。

最终报告相对冻结原始基线；搜索替换相对 incumbent；若以通用 Profile 开始，还必须报告相对通用 Profile 的增量。

## 停止

任务必须记录明确 stop reason。允许类别包括目标达成、搜索空间结束、经确认统计规则下收敛、预算耗尽、安全停止、无法恢复和用户取消。具体数值尚未校准。

## 不做

- 不让组件内优先级替代业务目标。
- 不根据一次相关性自动认定根因。
- 不在第一阶段部署常驻在线调参或阶段切换器。
- 不因未来可能缓存而跳过当前真实复测。

---

# 动态相位设计草案（PKG-G，2026-08-23）

> 以下为 overall.md §3.2/§3.3（动态优化与结束门禁）的展开设计。只出设计不写
> 实现；全部数值为占位符（待校准）；标 open 的条目按提案制等用户确认后才可
> 进入公式登记表与代码。

## D0. 负载供给边界：基础套件的双重角色（用户定位 2026-08-23，SO-D020）

M3 阶段没有真实业务应用，用 stress-ng / sysbench / fio / iperf3 等基础套件充当
workload（业务负载替身）。**同一批工具在两条相位里角色不同，边界写死**：

| 相位 | 谁启动压力工具 | 工具角色 | 引擎行为 |
|---|---|---|---|
| 静态（M2，已实现） | **优化器主动调用**——L3 压力器按 StandardPressureProtocol 的 prepare/warmup/measure/verify/cleanup 阶段合同驱动 | 受控探测负载 | 引擎经 L3 加压后由 L4 采集 |
| 动态（M3，本设计） | **测试/操作侧外部启动**并维持（测试 harness 或操作者按 workload 合同起压） | 业务负载替身——"测试给的压力" | 引擎**永不主动调用**压力工具；只观测（O0/O1）→ 打分 → 小步干预系统配置 → 复验 |

为什么必须这样切：

1. **可比性（S0 的动态版）**：负载由外部按合同提供，基线窗、观察窗、复验窗
   看到的是同一 `workload_identity_digest` 的负载；若引擎自己起压，任何配置
   干预都可能同时改变负载本身，改善量归因被污染。
2. **防自证**：引擎若既能造负载又能评收益，等于自己出题自己改卷。负载外置后
   引擎唯一能动的只剩系统配置，收益只能来自配置。
3. **生产语义对齐**：真实场景里业务方拥有 workload，优化器只能在业务之下调
   系统。stress-ng 替身保持这个方向：负载生命周期归"业务方"（测试侧）。
4. **观察者效应隔离**：O1 常态采集与配置施加都不触碰负载进程；负载的
   启动/停止/重启是外部事件，各自带测试侧证据记录。
5. **审计两侧分账**：负载启停属测试侧台账；引擎台账只含观测窗口与配置干预；
   两侧证据在 `workload_identity_digest` 上汇合。

**workload 合同相应新增字段（提案）**：

- `load_provider: external-test`——第一版只有这一种；**不提供**引擎自起压的
  模式（`load_provider=optimizer` 不进合同枚举）。
- `load_command_identity`——工具+参数+时长的身份摘要，由测试侧声明、观察窗
  核对；引擎持有它只为验身份，不因此获得执行权。
- O0 业务指标 = **读取外部负载自身的产出**（如 stress-ng 的 bogo-ops 统计、
  sysbench 的 tx 计数、fio 的 iops/lat 输出），引擎只解析产物，不启动进程。

对 D3（S9 复验窗）的影响：复验窗要求测试侧**重新提供同一身份的负载**——引擎
发出"复验窗请求"（附 `load_command_identity`），由测试侧起压；测试侧无法重供
（身份漂移/负载消失）→ 走 D4 `identity_drift_policy`，晋升 fail-closed。

对 D6 的影响：**动态引擎循环中不存在任何 L3 调用路径**；L3 压力器仍是静态相位
专用（PKG-B 压/采解耦同样只服务静态相位）。

## D1. 观测合同（O0–O3）与采集开销 A/B

**观察窗口（ObservationWindow）**：动态相位的基本观测单位。字段提案：

```
ObservationWindow:
  window_id            # 时间块标识，进 S9 复验的 time_block_id
  phase                # workload 阶段（manifest 显式声明；自动识别 open）
  o0_business:  list[metric_sample]   # 合同业务指标
  o1_system:    ComponentMetricSnapshot（L4 builtin 集）
  o2_windows:   list[ComponentCollectionRun]  # 仅被路由选中的组件
  o3_records:   list[authorization_scoped_trace]  # 显式授权时间盒
  workload_identity_digest  # 输入+阶段+规模的身份（S0 可比性的动态版）
  overhead_digest           # 指向本轮启用的各观察层开销证据
```

- 每个观察窗口携带自己的 `workload_identity_digest`；相邻窗口身份漂移超过任务
  声明容差 → 触发 S10 的"负载消失/剧变"停止类评估（不是静默继续）。
- **开销 A/B 复用 L4 已有合同**：`build_collection_overhead_evidence`（成对
  裸墙钟、无阈值无裁决）。O2 开窗与 O3 授权必须各自携带开销证据 digest；
  开销证据只记录不裁决——"开销是否可接受"是任务输入，不内置默认。
- 观察与施加分离（overall §3.2 干预约束）：一个窗口内**要么纯观察要么含一次
  干预后的复测**，不混"边改边看"。

## D2. S3 动态路由：症状 → 多组件假设（open decision #4 的提案）

现状：S3 只有静态侧雏形（`diagnostic_priorities → routed_components`，未真跑）。
动态相位提案——**假设是一等记录，不是一次路由调用**：

```
ComponentHypothesis:
  hypothesis_id
  symptom: SymptomRecord          # O0 业务退化/未达 SLO + 触发窗口 window_id
  component: cpu|memory|network|storage|numa
  rank: 由 S4 二维优先级 (P_m, D_m, Persistence, Confidence) 排出
  supporting_o1_o2_digests: list  # 支持证据（区分 O1 粗证 / O2 微证）
  competing: list[hypothesis_id]  # 竞争假设（同一症状的其他组件解释）
  status: proposed → probing → confirmed | refuted | superseded
  refute_evidence_digest         # refuted 时必填（干预无改善/O2 反证）
```

规则提案：

1. **一个症状至少登记两个竞争假设后才允许干预**（防单次相关归因，对齐"不做"
   条款）；假设数低于 2 时只允许 O2 开窗取证，不允许改配置。
2. `confirmed` 的唯一路径是**干预实验**：单组件小步干预 → 同 workload 协议
  复测 → 业务指标（不是组件微指标）给出 S7 裁决。O2 证据只能把假设推进到
  `probing`，永远不能直接 `confirmed`。
3. `refuted` 假设写入 L7 负缓存（身份 = 环境 × 组件 × 症状类 × 公式版本），
  后续同症状路由自动降优先级；这与现有"候选参数负缓存"共用 L7 骨架但身份
  分量不同——需要 L7 增加**第二种条目类型**（open：schema 版本如何并存）。
4. 路由输出不是单一组件，而是**假设队列**：预算按 S4 排序切分给前 K 个假设
  （K 为任务输入）；引擎逐个 probing，confirmed 即止或队列耗尽走 S10 收敛停止。

## D3. S9 复验观测生产路径（闭合 M11：现 passed 恒真、无真实复验生产者）

现状：`evaluate_promotion` 合同要求跨时间块/跨环境复验，但当前唯一观测源是
引擎轮内终裁（`passed` 复用同轮 verdict，恒真）。设计提案——**复验窗口作为
`VerificationObservation` 的真实生产者**：

```
VerificationWindow（复验窗口）:
  promoted_candidate_id      # 待晋升候选
  window_id                  # → VerificationObservation.time_block_id
  workload_identity_digest   # 必须与候选采纳轮 S0 可比
  outcome:
    passed: bool             # = S7 接受条件对【业务主指标】的裁决结果
    evidence_digest          # → 本窗口 MeasurementBatch digest
```

- **静态相位**（现有 engine-round 观测）：保留为"采纳记录"性质；晋升合同
  `min_observations` 与 distinct time blocks 的要求意味着仅靠轮内观测天然
  不够，必须等动态复验窗口补足——这一约束已实现（PromotionContract），本设计
  只补生产者，不改合同。
- **动态相位**：晋升候选进入"保留观察"状态，其后每个验证窗口对同一
  `workload_identity` 重测（负载由测试侧按 D0 重新提供，引擎只发复验窗请求）；
  `passed` 由重测批次的 S7 裁决产生（可为 false），
  失败观测走 `evaluate_promotion` fail-closed → 不晋升 + 触发 L6 候选级回退。
- 复验窗口计入结束门禁预算（防"无限复验"）：复验窗口数 ≤ 任务输入上限，
  超限走 S10 收敛停止，best-observed 以未晋升状态如实报告。

## D4. 结束门禁参数化合同（overall §3.3 五类停止的合同化）

提案 `DynamicPhaseGateContract`（**全部字段任务注入，无默认值**；合同 digest
进入证据身份，改参数即新身份）：

| 字段 | 对应停止类（S10） | 语义（数值待校准） |
|---|---|---|
| `slo_target + hold_windows N` | 目标达成 | 业务指标达标并保持 N 个连续观察窗口 |
| `convergence_rounds K + lcb_threshold` | 收敛 | 连续 K 轮候选业务收益 LCB ≤ 阈值 |
| `max_interventions / wall_clock_budget / risk_quota` | 预算 | 干预次数/墙钟/风险额度任一耗尽 |
| `degradation_gate`（业务退化显著性的任务声明） | 安全触发 | 任一变更致业务显著退化 → 回滚并停止本相位 |
| `identity_drift_policy`（workload 身份漂移容差） | 负载消失/剧变 | 漂移超容差 → 当前证据链失效，停止 |

防振荡补充（提案）：

- **迟滞**：结束门禁触发后，`reactivation_holdout`（任务输入）时间/窗口内
  不得重激活；
- **单窗单改**：每窗口至多一次配置变更（overall §3.2 已有，落进合同校验）；
- 停止记录必须引用：触发的合同字段 + 触发时的证据 digest + 当时假设队列状态
  （哪些 confirmed/refuted/open）——保证"为什么停"可回放。

## D5. 重激活判据提案（overall §10 open #2，三案等用户选）

| 案 | 判据 | 优点 | 缺点 |
|---|---|---|---|
| A 身份漂移 | workload_identity_digest 变化超 `reactivation_identity_tolerance` | 确定性、证据绑定、最便宜 | 需要任务合同暴露身份特征；"同身份但强度变"会漏 |
| B SLO 持续违反 | 曾达标后业务指标连续 `reactivation_slo_windows` 窗违反 SLO | 直接对准目标函数、带迟滞天然防噪 | 只盯 SLO 会漏成本类回退机会 |
| C O1 分布漂移 | O1 指标分布做统计漂移检验（如 PSI/分位数移动）超校准阈 | 最敏感、覆盖非 SLO 退化 | 需校准数据；误激活风险最高（振荡源） |

**推荐**：A + B 组合先行（身份漂移 → 立即具备重激活资格；SLO 持续违反 →
迟滞后具备资格），C 列为 M6+ 候选（等有校准数据再评估）。重激活一律消耗
`reactivation_budget` 并重置结束门禁计数，全程记决策日志；**重激活资格 ≠ 自动
重启**——是否重开相位由任务所有者决定（对齐"不做常驻自治"红线）。

## D6. 与现有实现的差距映射

| 设计件 | 现有雏形 | 缺口 |
|---|---|---|
| O1 观察窗口 | L4 `BuiltinLinuxGuestCollector` / `ComponentMetricSnapshot` | 窗口编排器、O0 业务指标接入、workload_identity_digest |
| 开销 A/B | `build_collection_overhead_evidence`（L4 合同） | 动态相位把它接进观察窗口的 overhead_digest |
| 假设路由 | S4 `diagnostic_priorities`（静态侧） | ComponentHypothesis 记录、竞争假设登记、L7 第二条目类型（open） |
| S9 复验生产者 | `PromotionContract`/`evaluate_promotion`（合同齐） | VerificationWindow 执行器 + passed 由重测 S7 产生 |
| 结束门禁合同 | S10 `StopReason` 枚举 + 静态相位门禁 | DynamicPhaseGateContract 模型与校验 |
| 重激活 | 无 | 全新；等 A/B 案确认 |
| workload 合同 | `OptimizationMode.WORKLOAD` + diagnostic-reference 入口 | 业务目标/SLO/阶段/输入身份的显式合同 schema + D0 负载供给字段（load_provider=external-test、load_command_identity）；O0 解析器读外部负载产物（stress-ng/sysbench/fio/iperf3 输出，引擎只解析不启动） |

依赖顺序建议：workload 合同 → O0/O1 观察窗口 → 门禁合同 → 假设路由 →
复验窗口 → 重激活。前四项不依赖 GPT PKG-B（L4 解耦）落地；复验窗口的
供数路径受益于 PKG-B（主指标走 L4 解析）但可先用现有 MeasurementBatch 路径。
