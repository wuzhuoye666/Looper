# Looper CPU Selection Pilot Design

状态：设计已完成，待执行授权  
证据与报价时间：2026-08-21  
约束：本文不授权创建任何云资源

## 1. 决策问题

首个试点回答两个采购问题：

1. 在相同的 8 vCPU / 32 GB 标称规格下，腾讯云 `S9.2XLARGE32` 与 `SA9.2XLARGE32` 哪一个能以更低成本满足内存驻留银行事务的吞吐和 p99 SLO？
2. 两个具体 SKU 在生产启发的单 VM 闭环 Web 全栈 workload 下各自能处理多少成功请求；该结果是否与数据库场景方向一致？

试点不输出一个 CPU 综合分，也不把两个 SKU 外推为所有 Intel/AMD 实例。每个场景分别报告性能、尾延迟、错误、稳定性和价格效率，并给出 Pareto 结果。

## 2. 场景选择

### 2.1 主场景：BenchBase SmallBank + PostgreSQL

用户问题：中小型数据库服务器承载短事务、热点账户和并发更新时，在 p99 SLO 下能提供多少有效事务容量？

选择原因：

- SmallBank 模拟银行账户上的简单读取与更新；每个事务访问少量 tuple，并包含热点访问。
- BenchBase 支持 PostgreSQL，输出每种事务的延迟和整体吞吐。
- 相比 `TPC-C` 名称，SmallBank 可避免把未按 TPC 官方规则审计的结果误称为正式 TPC-C 成绩。
- 数据规模可由 `scaleFactor × 1,000,000 accounts` 明确控制，适合构造超出 LLC、但仍主要驻留内存的 CPU/DB 场景。

固定语义：

| 参数 | 试点值 |
| --- | --- |
| BenchBase source | `cmu-db/benchbase@33c00473807ebd49304d114a6d769d2d2b2bbb34` |
| Workload | `smallbank` |
| DBMS | PostgreSQL 16，精确容器 digest 或包版本在执行前锁定 |
| Isolation | `TRANSACTION_SERIALIZABLE` |
| Scale factor | `10`，即 10,000,000 accounts |
| Transaction weights | `15,15,15,25,15,15`，保持 upstream sample mix |
| Client terminals | 初始 `256`；Stage 0 用 `128/256/512` 统一校准，锁定两端均不会触发 worker/connection ceiling 的最小共同值 |
| Arrival model | `poisson`；Stage 0 必须验证锁定 revision 的 rate limiter 确实实现所声明的 open-loop 语义 |
| Warm-up | 数据载入后 10 分钟；每个后续测量点至少 2 分钟 |
| Measurement | 每个测量点 10 分钟 |
| Pilot SLO | overall p99 ≤ 50 ms，unexpected error/timeout < 0.1%，serialization abort/deadlock/rollback/retry 总比例 < 1% |
| Goodput accounting | 只计已 commit 的成功业务事务；abort、deadlock、timeout、error 和 retry attempt 都不进入分子，并分别保留计数；timeout 来自客户端 accounting sidecar |
| Tail evidence | 每个 block 至少 100,000 个 measured-attempt latency samples；保存等价原始证据，并报告 p50/p95/p99/p99.9/max/timeout；不得把该 population 标成 committed-only |
| Durability | `fsync=on`、`synchronous_commit=on`、`full_page_writes=on`；不以关闭持久性换取 CPU 分数 |
| Memory / connections | 两端统一 `shared_buffers=8GB`、初始 `max_connections=320`；按最终共同 terminals 留足连接余量，其余 PostgreSQL 参数锁定并披露 |

`50 ms` 是试点验收阈值，不是对所有银行业务的推荐值。正式产品应让用户选择 SLO，并保存其来源。锁定 revision 的 `ResultWriter.writeRaw` 对所有 measured attempts 记录 latency，但不写 transaction outcome；因此初始试点的 tail SLO 明确作用于 all-attempt population。若要发布 committed-only tail，必须先加入并审核 outcome-tag patch，不能从 stock raw 文件反推。

#### SLO frontier procedure

两个目标使用同一搜索算法、容差、最大点数和 block 规则；公平性不要求它们只能测相同负载点。

1. Stage 0 先用 fixture 验证 `poisson`、`rate=unlimited`、terminal ceiling、延迟计时起点和所有错误映射。
2. 每个目标做 3 次短 `rate=unlimited` 校准；每次 2 分钟稳定、3 分钟测量，校准不进入正式比较。
3. 以两个目标各自 3 次 committed TPS 中位数中的较小值作为共同参考 `R`，在 `50% R`、`75% R`、`100% R` 画共同负载曲线。
4. 对每个目标分别找到一个通过 SLO 的下界和一个不通过的上界；使用相同的 bracket 扩展规则，不能因为已知目标身份人工选点。
5. 在 bracket 内做二分搜索。每个候选点先测 3 个 block，边界点扩展到 5 个；至少 4/5 block 同时满足 p99、goodput、abort、error、timeout、offered-load achievement、rate-limiter lag 和 client-headroom gate 才判定通过。
6. 当上下界宽度不超过通过下界的 `2.5%` 时停止；每个目标最多增加 5 个自适应负载点。预算耗尽或始终找不到双边 bracket 时，结果是 `frontier unresolved`。
7. 主结果是 `slo_frontier_interval = [highest confirmed pass, lowest confirmed fail]`，不是有限网格上的最高点。中点只用于绘图；价格效率使用保守的 confirmed-pass 下界。
8. 只有两个 frontier interval 的保守差异超过 5%，且满足第 6 节其他门槛，才可声明容量可区分。

同时报告每种事务的 committed throughput、all-attempt p50/p95/p99/p99.9/max、abort/deadlock/rollback/retry/timeout、客户端 planned/offered/started/completed rate、rate-limiter lag、headroom、排队和资源利用率。planned load 来自锁定配置，BenchBase summary throughput 只能记为 actual attempted throughput。禁止对 5 个 per-run p99 再计算一个 p99；同一 target/load/placement 的直方图可以按预先规定的等价窗口合并，block summary 仍作为重复证据保留。

上游 `Phase` 当前使用未显式传入 seed 的 Java `Random`。P1 必须选择并披露以下一种做法：加入经过审核的 seed patch，或把 workload 顺序随机性作为重复噪声而不声称逐请求配对。应用层自动 retry 默认关闭；若 upstream 无法关闭，必须把原始 attempt、完整重试链和最终 committed transaction 分开记账。

### 2.2 次场景：DCPerf MediaWiki 单 VM 闭环全栈容量

用户问题：每个具体 SKU 把 load generator、Web server、应用和数据库共同放在一台 VM 时，能完成多少成功请求并维持怎样的尾延迟？

固定语义：

| 参数 | 试点值 |
| --- | --- |
| DCPerf source | `facebookresearch/DCPerf@9308c3e3c404e0466f0a2929f15ddcf62b2215f6` |
| Job | `oss_performance_mediawiki_mlp` |
| Platform | Ubuntu 22.04 x86_64，upstream 明确列为支持平台 |
| Load generator | `wrk`，不用存在已知 deadlock 风险的 Siege |
| Scale-out | `1`，两个 8-vCPU 目标相同且不触发 >100 logical CPU 自动扩展 |
| Measurement duration | 10 分钟 |
| Repeat count | 每个目标 5 个有效 measured repeats，另有 1 次不计分 smoke/warm-up |
| Primary metrics | successful requests/s、p50/p95/p99、failed request ratio |
| Validity checks | upstream job 成功；最后测量窗口 CPU 利用率 ≥ 90%；端口/服务健康；无安装或 parser 错误 |

该 workload 把 Web server、数据库和 load generator 放在目标内，结果只代表一台实例上的闭环全系统容量。共置组件会竞争 CPU、cache、memory 和 scheduler；即使 CPU 利用率达到 90%，也不能把结果解释为纯服务端、纯网络或纯 CPU 容量。报告必须明确包含客户端开销，次场景排名只用于检查具体 SKU 在另一种整机 workload 下的方向，不验证数据库场景的 CPU 归因。

#### Fallback

如果锁定 revision 的 HHVM 3.30 或 MediaWiki 依赖不能在干净 Ubuntu 22.04 镜像上重复构建，则结果记为 availability failure，不得临时换版本后沿用同一 benchmark identity。经过新 revision/manifest 审核后，可用 `django_workload_default` standalone 作为 fallback。TaoBench 不作为低成本首轮 fallback，因为 upstream 推荐三台机器、CentOS Stream 8/9、10–20 Gbps 网络和较长 warm-up。

## 3. 腾讯云目标矩阵

### 3.1 主比较组

| 角色 | 配置 | 公开处理器事实 | 区域/可用区 | 镜像 | 磁盘 | 2026-08-21 按量询价 |
| --- | --- | --- | --- | --- | --- | --- |
| Target S9 | `S9.2XLARGE32`，8 vCPU / 32 GB | 规格页声明 Intel Sierra Forest、DDR5；S9 家族描述睿频 2.7 GHz，实际 guest 信息必须复核 | `ap-guangzhou` / `ap-guangzhou-6` | `img-487zeit5` Ubuntu Server 22.04 LTS x86_64 | 100 GB `CLOUD_PREMIUM` system disk | ¥1.79 / hour |
| Target SA9 | `SA9.2XLARGE32`，8 vCPU / 32 GB | 规格页声明 AMD EPYC Turin-Dense、DDR5；SA9 家族描述全核睿频 3.4 GHz、支持超线程开关，实际 guest 信息必须复核 | 同上 | 同上 | 同上 | ¥1.62 / hour |
| Fixed client | `S9.4XLARGE32`，16 vCPU / 32 GB | 只驱动 BenchBase，不进入目标排名 | 同上 | 同上 | 50 GB `CLOUD_PREMIUM` system disk | ¥2.31 / hour |

广州五、六、七区在查询时均列出 S9/SA9 8-vCPU/32-GB 配置。选择六区是因为两种当前机型和 S5/SA5 旧代际都被目录列出，便于将来增加纵向组。目录和询价成功不保证创建时库存。

### 3.2 已知非等价项

“同 vCPU/内存”不代表硬件完全相同：

- CPU 厂商、微架构、频率、SMT 实现和缓存不同；这些正是采购比较因素，不能被消除。
- 官方规格页在相同小规格上给 S9 与 SA9 不同的网络 PPS/带宽承诺。BenchBase 使用外部客户端时必须证明目标实际网络低于较小承诺的 20%，否则结果标为 CPU/network mixed。
- 云宿主、超分、steal 和物理拓扑是黑盒变量，单次创建不能代表实例族。
- 100 GB 高性能云硬盘控制了盘型和容量，但不能保证两个实例位于相同性能后端。

因此报告必须展示实际环境和资源曲线，结论限定为该时点、该 region/AZ、镜像和计费请求下的两个具体 SKU。不能把差异直接归因于“Intel vs AMD”，也不能外推整个实例 family；目录/询价成功和公开处理器描述都必须由创建后的 guest fingerprint 复核。

### 3.3 可选纵向组

只有主组完成后才考虑 `S5.2XLARGE32` 与 `SA5.2XLARGE32`。同样的 8 vCPU/32 GB、50 GB 磁盘询价分别约 ¥1.84/小时和 ¥1.75/小时，而腾讯推荐接口已把旧 S5/S6/SA5 引向 S9/SA9。旧组用于代际收益研究，不应替代当前采购主组。

## 4. 公平性协议

### 4.1 必须相同

- Region、AZ、VPC、subnet、镜像 ID、系统盘类型/容量、计费类型和运行时间块。
- Benchmark source revision、wrapper revision、PostgreSQL image digest、JDK、Docker/containerd 和依赖 lock。
- Dataset、scale factor、事务 mix、offered load、warm-up、测量时长、SLO、客户端和请求顺序策略。
- OS 更新状态、kernel、cgroup、ulimit、THP、NUMA policy、swap、time sync 和安全组语义。
- 无 per-target 编译 flag、数据库参数、绑核、governor 或功耗策略。

只允许 CPU/实例族本身及云平台随实例提供的默认硬件差异变化。

### 4.2 环境指纹

每次创建、每次 reboot 后和每个 measured block 前后采集：

- `lscpu --json`、`/proc/cpuinfo` model/flags/microcode、online CPU、SMT、cache 和 NUMA topology。
- OS image ID、kernel、boot cmdline、container runtime、JDK、PostgreSQL、benchmark 和 wrapper digests。
- CPU governor/EPP/cpufreq（若 guest 可见）、TuneD/PPD/TLP、THP、swap、cgroup v1/v2 和 quota。
- memory total/available、disk model/mount/fs/options、network interface/MTU/routes、virtualization type。
- steal time、host-visible clock behavior、NTP offset；无法读取的字段显式为 `unavailable`。
- CVM instance ID、family/type、region/AZ、creation time、image、VPC/subnet、advertised network limits和价格快照。

现有 `services/worker/looper_worker/fingerprint.py` 只记录基础平台、CPU 数和内存，P1 必须扩展后才满足此协议。

### 4.3 隔离与瓶颈门槛

BenchBase 客户端只有在以下条件同时成立时才有效：

- 客户端 CPU p95 < 60%，无明显 steal 或 throttling；内存无 swap，连接池无饥饿。
- `offered → started → completed` 全链路计数可对账；rate-limiter lag < 1%，可用 terminal/connection headroom ≥ 20%。
- Stage 0 把 terminals 从 `128 → 256 → 512` 统一扩展；如果扩展后同一 offered load 的 goodput 或 p99 变化 ≥ 2%，说明客户端/worker ceiling 尚未排除。
- 目标和客户端网络吞吐/PPS均低于两目标较小公开承诺的 20%。
- PostgreSQL CPU 已达到可解释区间，或报告证明限制来自锁/WAL/磁盘而非客户端。
- disk latency、IOPS 和 queue depth 全程采集；若一端出现盘限速，场景标为 mixed resource，而非 CPU 胜负。

DCPerf MediaWiki 的 client 与 server 共置是 workload 定义的一部分，不套用外部客户端门槛。

## 5. 重复、配对和随机化

### 5.1 单 placement 初始比较

- S9 与 SA9 在同一 creation wave 创建，形成一个 `placement_pair_id`；两台尽量同时保持运行。
- 每个场景先做 1 次不计分 smoke/warm-up。
- 每个 MediaWiki 目标做 5 次 measured repeat；可在同一时间窗并行运行，保存共享 `time_block_id`。
- SmallBank 的三个共同 offered-load 点各先做 3 个 paired blocks；若某点成为 frontier 边界，扩展到 5 个。
- 固定客户端按预注册的平衡顺序驱动共同负载点，例如 `ABBA` 后接 `BAAB`；顺序 seed 写入 run envelope。
- 两目标的自适应 frontier 点可能不同，不能伪称逐点配对；它们使用相同算法并交错执行，以降低时间漂移。
- 每个 block 前恢复相同数据库 seed state；恢复方法、摘要和耗时进入证据包。
- 不删除失败 attempt，不以 retry 的成功结果覆盖首次失败。

### 5.2 Placement 变异阶段

初始比较只能证明一个 placement pair 上的差异。变异阶段要求：

- 每个 wave 同时创建一个 S9 和一个 SA9，形成配对；先做 3 个独立 placement pairs。
- 创建时间跨至少两个时间块；每个 pair 都执行相同 frontier procedure 和至少 3 个有效 block。
- placement 内 repeats 只估计运行噪声，不能当作更多独立 placement 样本。
- 统计以 `placement_pair_id` 为 cluster/推断单位，报告每对 effect、placement 内 CV、placement 间方差、environment sensitivity 和 rank stability。
- 3 个 placement pairs 只允许发布 `multi-placement exploratory SKU result`。不得外推 instance family 或 CPU vendor。
- 根据前三对的 placement 方差做功效分析，预注册达到 80% power、检测 5% effect 所需的后续 pair 数；正式 SKU 采购建议的下限不得少于 5 对。

如果预算只允许一组实例，结论必须标为 `single-placement provisional`。

## 6. 统计与区分度门槛

对每个场景分别计算：

- 原始 latency histogram 和 block-level mean、median、min/max、standard deviation、CV；不能把 per-run p99 当作原始 latency sample。
- 共同负载点使用真正的 paired-difference bootstrap：按 `time_block_id` 成对重采样，输出 95% interval。
- 多 placement 结果先在每个 pair 内聚合，再按 `placement_pair_id` 做 paired/cluster-aware 分析；禁止把 pair 内 repeats 扁平化为独立样本。
- 每个 time/placement block 的排名和 rank stability；environment sensitivity 只作为诊断，不替代 SKU effect interval。
- SLO frontier 的 pass/fail bracket、宽度和未决状态；不把 bracket midpoint 当成精确观测。
- cost efficiency：用 confirmed-pass 下界计算 SLO goodput per CNY/hour、successful requests per CNY，以及每百万 committed 操作估算成本。

当前 `looper_core.analysis.bootstrap_improvement` 对两组样本独立重采样，不是 paired bootstrap，不能用于本文的 paired 结论。P1 必须新增按 pair ID 重采样的 helper 和 placement-cluster fixture。

单 placement 下，`distinguishable` 必须同时满足：

1. 两个目标均通过 correctness/validity、tail sample、client headroom 和 resource bottleneck gate。
2. 两个 `slo_frontier_interval` 已解析，且保守区间表明容量差异至少 5%。
3. 共同负载点的 paired 95% interval 不跨 0，且 effect 方向与 frontier 一致。
4. 至少 80% 的共同 paired blocks 排名一致。
5. 单 placement 内主指标 CV 不高于 10%；超过则先报告云变异，不能强行排序。
6. 没有客户端、网络、磁盘、abort/error 或环境指纹差异解释掉结果。

否则结论是 `not distinguishable at current budget`、`frontier unresolved` 或 `confounded`，不是“性能相同”。即使通过这些门槛，单 placement 仍只能是 provisional；多 placement 结论遵守第 5.2 节的独立样本和功效规则。

## 7. 归因层

主报告先给场景结果，再给归因；不把归因微基准汇总成用户总分。

优先采集：

- CPU utilization、user/system/steal/iowait、run queue、context switch 和 migration。
- `perf stat`（guest PMU 可用时）的 cycles、instructions、IPC、branches、cache/TLB misses。
- PostgreSQL wait event、lock、WAL、buffer hit、checkpoint、transaction/rollback 和 query latency。
- disk bandwidth/IOPS/await/queue、network bytes/packets/retransmit。
- 温度、功耗和物理频率若 guest 不可见则记 `unavailable`，不估算。

只有场景差异无法解释时，才追加 STREAM 或其他 CPU/memory probe。probe 只验证机制，不改变场景排名。

## 8. 成本上限

询价是按量、100% 报价响应，未含未来价格变化、优惠、快照、镜像存储、公网流量或失败重跑。执行前必须刷新。

单 placement 最坏时长 ledger：prepare/image verification 3h、数据库 load/snapshot 2h、terminal 与 unlimited calibration 1.5h、共同负载点约 4h、自适应 frontier 最多 8h、MediaWiki 约 2h、artifact/teardown 1.5h。SmallBank 由一个固定客户端顺序驱动两端，不能按目标并行后继续声称客户端已配平。因此为 targets 设置 24h、client 设置 16h 硬上限；实际 DAG 未消费的时间立即 terminate。

| 阶段 | 资源时间硬上限 | 计算价格 | 25% reserve 后审批上限 |
| --- | --- | --- | --- |
| A：单目标 smoke | SA9 target 6h + fixed client 4h | `1.62×6 + 2.31×4 = ¥18.96` | `¥23.70`，建议审批 `¥25` |
| B：单 placement 对比 | 两 targets 各 24h + client 16h | `(1.79+1.62)×24 + 2.31×16 = ¥118.80` | `¥148.50`，建议审批 `¥150` |
| C：达到 3 placement pairs | 在 B 之外再做两个同等 wave | `¥237.60` 增量 | `¥297.00` 增量；A+B+C 累计建议上限约 `¥472`，向上审批 `¥475` |

Stage C 仍只产生 multi-placement exploratory SKU result。达到正式采购建议所需的后续 pair 数由前三对的方差与功效分析决定，不在 `¥475` 内。自定义镜像和缓存可降低时长，但不能以减少有效 block、缩窄 frontier 规则或省略 placement 为代价后声称已验证公平性。预算耗尽时完整保留已计划/已完成矩阵，结论标为 incomplete，禁止只发布有利目标的已完成部分。

执行时每个资源都必须带 goal/run/owner/expiry 标签，设置硬截止时间，证据上传后立即 terminate；仅 stop 仍可能产生磁盘费用。

## 9. P1 执行前置条件

当前代码不能直接执行该试点：

- `services/api/looper_api/cloud_adoption.py` 把云实例采纳为 `inventory-only`、`runnable=False`。
- worker runner 只执行本机 trusted local-process benchmark。
- 当前 fingerprint 不满足公平性字段。
- 创建实验仍以 candidate search/objective/gate/Pareto 优化为中心，没有 scenario、role、SLO frontier 和 target comparison 契约。

最小 P1 前置工作：

1. 增加 `ScenarioBenchmark` 契约：用户问题、角色拓扑、SLO、offered-load、correctness gate、goodput/error accounting 和 attribution probes。
2. 支持在 CVM 上 bootstrap/注册 ephemeral worker，或实现等价的受控远程 runner；每个命令仍用 argv、超时、最小权限和 artifact allowlist。
3. 扩展 target snapshot 和 fingerprint，保存本文第 4.2 节字段，并增加 immutable price/inquiry snapshot。
4. 实现 BenchBase 和 DCPerf adapter；prepare/run/validate/cleanup 分离，source/container digest 固定；BenchBase Stage 0 fixture 验证 Poisson/open-loop、terminal、latency 起点和错误映射。
5. 实现 bracketed SLO-frontier search、pass/fail interval、最大点数和 budget-aware incomplete 状态。
6. 增加 raw/HDR latency histogram 契约、最低样本量、p99.9/max/timeout 规则；禁止对 per-run quantile 再取 tail quantile。
7. 实现 paired block、placement-pair schema、随机顺序、数据库 seed restore 和失败保留。
8. 新增真正的 paired-difference bootstrap 和 placement-cluster analysis fixture；现有独立 `bootstrap_improvement` 不得替代。
9. 实现 committed goodput、abort/deadlock/rollback/retry/timeout 的独立账务与 hard gate。
10. 从 run DAG 生成逐资源 duration ledger、预算消耗和硬 expiry；任何 RunInstances 调用前重新询价并要求显式预算/资源确认。
11. 生成 target comparison 报告，不再把参数 candidate/Pareto optimizer 当作主产品流程；结论强度随 placement evidence 显式分级。

## 10. 阶段验收

- Stage 0（无云）：adapter/source/license、BenchBase 语义、SLO search、histogram、goodput accounting、paired/cluster statistics、budget ledger 和 teardown fixtures 全部通过。
- Stage A：只证明一台 SA9 能安装、运行、校验、采集 artifact 并清理；不发布性能排名。
- Stage B：发布 `single-placement provisional SKU comparison`，必须通过公平性和区分度门槛。
- Stage C：三个 placement pairs 后只发布 `multi-placement exploratory SKU result` 和方差/功效分析，不发布 family 或 CPU vendor 建议。
- Stage D：按功效分析完成预注册 pair 数后，才可考虑具体 SKU 的采购建议；下限不少于 5 pairs。
- 任一阶段资源、证据或清理失败，停止进入下一阶段。

## 11. 公开证据

- Tencent Cloud CVM instance specifications: https://cloud.tencent.com/document/product/213/11518
- BenchBase: https://github.com/cmu-db/benchbase
- SmallBank description: https://github.com/cmu-db/benchbase/wiki/SmallBank
- DCPerf: https://github.com/facebookresearch/DCPerf
- DCPerf MediaWiki guide: https://github.com/facebookresearch/DCPerf/tree/main/packages/mediawiki
