# System Optimizer 指标与字段全量盘点（讨论稿）

> 状态：资料处理阶段，2026-08-22。本文不是最终指标协议，不代表字段已经筛选、合并或进入实现。
>
> 计数口径：论文资料按 `papers/fulltext/` 下的实际文件路径计数，不按标题、arXiv ID 或内容去重；Looper 指标按 `benchmark.yaml` 中每个 `文件路径 + metric id` 声明计数。相同名称不默认语义等价。

## 1. 已确认的产品语义

系统优化器使用同一套配置采集、施加、验证、测量和回滚底座，但包含两个测量阶段：

1. **标准压力调优**：没有真实业务压力，由受控组件探针产生 CPU、内存、存储、网络等压力；组件指标负责驱动、诊断和验收本组件调优。
2. **workload 场景调优**：workload 用作真实场景的效果验证和搜索目标，不负责反向定义通用组件指标体系。其业务指标负责判断配置在该场景下是否有效，系统组件指标负责解释为什么有效或退化。

用户手动配置、标准压力产生的候选、场景搜索产生的候选必须进入同一个安全执行闭环。本文暂不处理配置项选择和执行实现。

## 2. 原始资料清单与计数

### 2.1 `papers/fulltext/`

目录中共有 **24 个路径文件**：12 个 Markdown、11 个 PDF、1 个 TXT。以下文件全部保留在盘点范围内：

| 文本材料 | 对应或相关 PDF | 说明 |
|---|---|---|
| `2405.10170.md`、`a_paper_pdf.txt` | `2405.10170v4.pdf` | 均显示 MESS 标题；未做内容等价判定 |
| `2505.03600.md` | `2505.03600v1-TailBench++.pdf` | TailBench++ |
| `2510.15744.md` | `2510.15744v4Ramulater.pdf` | MESS/Ramulator 纠错论文 |
| `2605.02194.md` | `2605.02194v1.pdf` | IO500 统计分析 |
| `2605.03713.md` | `2605.03713v3-SPECCPU.pdf` | SPEC CPU2026 特征化 |
| `2605.06544.md` | `2605.06544v1-CCL-Bench.pdf` | CCL-Bench |
| `2607.01211.md` | `Are Performance-Optimization Benchmarks Reliably Measuring Coding Agents.pdf` | Benchmark 可信度审计 |
| `2607.14541.md` | `2607.14541v1-AreLLM-GeneratedGPU.pdf` | Atrex-Bench/AKA |
| `CloudyBench_ICDE25.md` | `CloudyBench_ICDE25.pdf` | CloudyBench |
| `DCPerf_fulltext.md`、`DCPerf_ASPLOS25_meta_blog.md` | `DCPerf.pdf` | 前者标注为论文全文；后者明确标注为 Meta 博客摘要，不能冒充论文原文 |
| `VGO_ICPE2026.md` | `VGO_ICPE2026.pdf` | VGO |

### 2.2 Looper 当前套件

- 5 份 `benchmarks/**/benchmark.yaml`。
- 42 条 metric 声明，计数键为 `benchmark.yaml 路径 + metric id`。
- 33 个不同的 metric id。这个数字只是字符串去重结果，**不表示同名字段语义已经验证等价**。
- 5 份 adapter manifest；其中 Atrex 和 DCPerf Benchpress 共包含 7 条显式 `metric_mappings`。
- CCL synthetic workload card 另有 2 条 metric catalog 声明。
- BenchBase、DCPerf MediaWiki、Atrex、CCL 现有 fixture 均明确标注 synthetic，不能当真实 workload 实测。

首轮 PowerShell 解析脚本曾报告 37 条，因为每份 YAML 的最后一个 metric 没有在退出 `metrics` 块时落入结果；修正 flush 逻辑后为 42 条。这是盘点脚本的技术性问题，未修改源数据。

## 3. 指标记录字段候选（待确认）

下面定义的是“指标资产表一行应保存什么”，不是已经批准的 schema。

| 字段 | 含义 | 当前处理规则 |
|---|---|---|
| `source_id` | 论文、benchmark 或 adapter 的稳定来源标识 | 必填；不从标题猜唯一性 |
| `source_type` | `paper` / `benchmark-manifest` / `adapter` / `fixture` / `schema` | 必填 |
| `source_path` | 仓库内实际路径 | 必填 |
| `source_locator` | 论文行号、表号、schema 路径或 JSONPath | 能定位时填写 |
| `raw_name` | 来源中的原始字段/指标名 | 原样保留 |
| `canonical_name` | 后续统一命名 | 当前统一填“待确认”，不擅自映射 |
| `component_candidate` | 候选组件分类 | 当前仅供讨论，不是最终分类 |
| `metric_role_candidate` | `component-objective` / `diagnostic` / `validity` / `workload-objective` / `guardrail` / `trust` / `environment` | 当前仅供讨论 |
| `unit_raw` | 来源原始单位 | 原样保留；`ms` 与 `millisecond` 不自动等价 |
| `direction_raw` | 来源原始优化方向 | 原样保留 |
| `statistic` | sample/mean/median/p50/p95/p99/CV 等 | 无明确声明则标未知 |
| `scope` | host/device/process/rank/client/workload/candidate/study 等 | 无证据不推断 |
| `phase` | warmup/measurement/validation/cleanup 或 workload 自身阶段 | 无明确阶段则标未知 |
| `collection_method` | perf/sysfs/trace/workload output/derived formula 等 | 区分直接采集与派生值 |
| `required_raw` | 来源是否要求该指标 | 保留来源语义 |
| `availability` | 当前 Looper 可采集、只有 fixture、论文提及、未知 | 不用论文数字冒充本地能力 |
| `semantic_status` | `verified` / `partially-verified` / `unverified` | 同名字段抽查前不得标 verified |
| `notes` | 限制、fallback、混杂、统计口径 | 必填关键风险 |

## 4. 论文原始指标盘点（未筛选、未合并）

### 4.1 Benchmark 可信度审计（`2607.01211.md`）

| 原始指标/字段 | 候选角色 | 原文语义摘要 |
|---|---|---|
| replay evaluability | validity | 每个任务在机器×轮次组合中能否得到可用结果 |
| correctness/equivalence pass | guardrail | patch 是否通过正确性/等价性检查 |
| base runtime、reference runtime | workload-objective/raw | 基线与参考 patch 的运行时间 |
| faster-than-base | validity | 参考 patch 是否在所有重放中快于基线 |
| original-rule valid | validity | 是否继续满足原 benchmark 的任务纳入规则 |
| speedup、runtime-change percentage | workload-objective/derived | 跨 benchmark 统一比较性能信号的派生量 |
| replay variation | trust | 跨机器、跨轮次的运行时变化 |
| OPT@1/reference-level pass rate | trust/score | GSO 的二元参考级通过率 |
| SpeedUp Ratio | trust/score | 提交 speedup 与参考 speedup 的比值 |
| harmonic-mean score | trust/score | SWE-efficiency 聚合规则 |
| per-task score weight/leverage | trust | 单任务对聚合分数分母的贡献 |
| rank、rank movement、pair flips | trust | 聚合规则改变后的排名稳定性 |
| Spearman rank correlation | trust | 两种排名的一致程度 |

这些字段适合成为调优结果的可信度门，不属于 CPU/内存等单一组件指标。

### 4.2 CCL-Bench（`2605.06544.md`）

论文明确列出的 metric toolkit 字段：

| 原始指标/字段 | 候选组件 | 候选角色 |
|---|---|---|
| average step time | accelerator/cross-component | workload-objective |
| MFU | accelerator-compute | diagnostic/objective |
| compute unit coverage | accelerator-compute | diagnostic |
| primary kernel timespan | accelerator-compute | diagnostic |
| host-device bandwidth | accelerator-memory/interconnect | diagnostic |
| memory-transfer overhead | accelerator-memory | diagnostic |
| collective bandwidth | accelerator-interconnect | diagnostic |
| communication fraction | accelerator-interconnect | diagnostic |
| compute-communication overlap | accelerator-interconnect | diagnostic，不能单独等价为 step time 改善 |
| MoE fraction | accelerator/workload | environment/diagnostic |
| TTFT、TPOT | accelerator-serving | workload-objective/guardrail |
| collective traffic volume by type | accelerator-interconnect | diagnostic |
| exposed/overlapped communication time | accelerator-interconnect | diagnostic |
| hardware-resource utility | accelerator/interconnect | derived objective；定义为资源翻倍后的 step-time 改善比例 |

论文还明确给出应保留的证据字段：operator、kernel、communication event、timestamp、rank、stream、collective type、group id、buffer/message size、model family、phase、batch size、sequence length、precision、iteration count、device/driver、network topology/bandwidth、framework/compiler、DP/TP/PP/CP/EP、microbatch、communication library/protocol。这些是 trace/workload context，不应混作性能指标。

### 4.3 DCPerf（`DCPerf_fulltext.md`）

| 原始指标/字段 | 候选组件 | 候选角色 |
|---|---|---|
| Peak RPS | workload | workload-objective |
| RPS under latency SLO | workload | workload-objective + guardrail |
| cache hit rate | application/cache | diagnostic/guardrail |
| workload throughput | workload | workload-objective |
| CPU utilization total/user/kernel/IRQ | CPU/OS | validity + diagnostic |
| memory utilization、swap | memory/OS | validity + diagnostic |
| network bytes/sec、packets/sec | network | validity + diagnostic |
| CPU core frequency | CPU-frequency | diagnostic/environment |
| power consumption and component breakdown | power | component-objective/diagnostic |
| frontend stalls | CPU-frontend | diagnostic |
| backend stalls | CPU-backend | diagnostic |
| incorrect speculation | CPU-branch/speculation | diagnostic |
| retiring | CPU-execution | diagnostic |
| IPC | CPU-execution | diagnostic/component-objective candidate |
| memory bandwidth | memory | diagnostic/component-objective candidate |
| cache misses、L1 I-cache MPKI | CPU-cache/frontend | diagnostic |
| kernel/user cycles | OS/CPU | diagnostic |
| application-logic/datacenter-tax cycles | OS/runtime | diagnostic |
| normalized benchmark score、geometric-mean overall score | suite | workload score/trust |
| Perf/Watt、Perf/$ | power/cost | workload decision metric |

### 4.4 VGO（`VGO_ICPE2026.md`）

| 原始指标/字段 | 候选组件 | 候选角色 |
|---|---|---|
| run time、mean、median、SD、CV、p95 | cross-component | workload-objective/stability |
| context switches | scheduler | diagnostic |
| thread affinity | scheduler/placement | environment/diagnostic |
| branch mispredictions | CPU-branch | diagnostic |
| cache misses | CPU-cache | diagnostic |
| CPU migrations | scheduler/NUMA | diagnostic |
| page faults、major page faults | memory/VM | diagnostic |
| dTLB misses | CPU-translation/memory | diagnostic |
| memory refresh rate | memory/hardware | environment/diagnostic |
| emulation faults | CPU/virtualization | diagnostic |
| L1 instruction-cache misses | CPU-frontend | diagnostic |
| CPU frequency | CPU-frequency | diagnostic/environment |
| compression bandwidth、energy use、power efficiency、response time、I/O time | workload/组件待定 | 文中列举的可优化 outcome，不能默认都由当前实现采集 |

VGO 证明的是“低层指标与性能分布区域的关联可指导缓解实验”，没有证明关联就是因果；进入自动调优前必须通过主动改配置和复测验证。

### 4.5 CloudyBench（`CloudyBench_ICDE25.md`）

| 原始指标/字段 | 候选角色 | 组成字段 |
|---|---|---|
| TPS | workload-objective | 平均事务吞吐 |
| P-Score | derived workload metric | TPS；CPU、内存、存储、IOPS、网络的值与单位成本 |
| E1-Score | elasticity metric | TPS；CPU、内存、IOPS 成本 |
| F-Score | failure guardrail | 故障注入时间、服务恢复时间 |
| R-Score | recovery guardrail | 服务恢复时间、恢复到目标 TPS 的时间 |
| E2-Score | scale-out metric | RO 节点数、节点增量、各节点数下 TPS、scaling factor |
| C-Score | replication guardrail | insert/update/delete replication lag、replica count |
| T-Score | multi-tenant metric | 各租户 TPS、各租户资源成本、租户数 |
| O-Score | unified score | P/T/E1/E2/R/F/C 与 scale factor |
| scaling interval、scaling cost | elasticity diagnostic | 时间槽、资源变化、成本变化 |

O-Score 允许各维度补偿。导师材料已经提出正确性、可用性、RPO/RTO、复制延迟和 SLO 应作为硬门禁，因此它只能保留为论文原始字段，不能默认成为 Looper 唯一目标。

### 4.6 Atrex-Bench/AKA（`2607.14541.md`）

| 原始指标/字段 | 候选组件 | 候选角色 |
|---|---|---|
| compile success | accelerator | validity gate |
| correctness across seeds/shapes | accelerator | correctness gate |
| candidate runtime/latency | accelerator | workload-objective |
| production baseline runtime | accelerator | comparison baseline |
| per-shape roofline time | accelerator-compute/memory | hardware-bound denominator |
| per-shape roofline achievement | accelerator | derived objective |
| per-operator median achievement | accelerator | derived objective |
| production-weighted aggregate score | accelerator/workload | workload score |
| operator importance weight | workload context | production GPU-time share × application card-hour share，按 phase 计算 |
| prefill/decode phase | workload context | scope，不是性能值 |
| FlyDSL adoption/target-DSL dominance | accelerator/code path | anti-fallback validity |
| FLOPs、bytes moved、arithmetic intensity | accelerator-compute/memory | diagnostic/roofline input |
| dtype compute peak、memory bandwidth peak | accelerator | environment/roofline input |
| kernel launch timing、stream、memory operation、correlation id | accelerator trace | raw evidence |

### 4.7 MESS 与 Ramulator 纠错（`2405.10170.md`、`2510.15744.md`）

| 原始指标/字段 | 候选组件 | 候选角色 |
|---|---|---|
| used/achieved memory bandwidth | memory | component-objective |
| memory access latency/loaded latency | memory | component-objective |
| unloaded latency | memory | baseline |
| read ratio、write ratio | memory workload | pressure input/context |
| memory intensity、NOP frequency | memory workload | pressure input |
| theoretical bandwidth | memory | environment/sanity bound |
| maximum achievable bandwidth | memory | derived sanity bound |
| saturated bandwidth range | memory | derived diagnostic |
| maximum latency range | memory | derived diagnostic |
| saturation point | memory | derived guardrail；MESS 采用 latency 达 unloaded latency 两倍 |
| row-buffer hit/empty/miss rates | memory/DRAM | diagnostic |
| dTLB/page-walk overhead | memory/translation | measurement-validity/correction |
| request count、simulation instruction count | measurement | validity/context |

纠错论文说明 simulator 与 real-system 若使用不同 workload 或错误统计字段会产生错误结论。因此 `latency`、`bandwidth` 等同名字段必须同时记录测量层级、统计来源和 workload，不能只按名字合并。

### 4.8 SPEC CPU2026（`2605.03713.md`）

论文 Table 3 明确列出 **19 个**微架构指标，逐项展开如下：

1. IPC；
2. L1I MPKI；
3. L1D MPKI；
4. L2 MPKI；
5. L3 MPKI；
6. L1 iTLB MPMI；
7. L1 dTLB MPMI；
8. L2 TLB MPMI；
9. Branch MPKI；
10. Frontend stall %；
11. Backend stall %；
12. Kernel instruction %；
13. User instruction %；
14. Load instruction %；
15. Store instruction %；
16. Branch instruction %；
17. FP instruction %；
18. Vector instruction %；
19. Memory access bytes/cycle。

这些指标属于 CPU/内存组件表征；论文在九个平台上将 19×9 形成 171 维特征再做 PCA。跨 ISA/微架构的 counter 可用于压力表征，但论文也提示不能把跨厂商绝对计数器值当严格等价量。

### 4.9 IO500（`2605.02194.md`）

原始/聚合性能字段：Overall Score、Score BW、Score MD、IOR-easy Write/Read、IOR-hard Write/Read、MDTest-easy Create/Stat/Read/Delete、MDTest-hard Create/Stat/Read/Delete、parallel-find rate。IOR 为 GiB/s，MDTest/pfind 为 kIOPS，不能因进入 geometric score 就视为同单位。

派生和日志字段：

- per-node、per-process normalized phase score；
- phase runtime；
- per-process operation start/end time；
- stonewall throughput、stonewall duration；
- close duration/close-time overhead；
- total runtime / stonewall duration ratio；
- wear-down duration与进程完成分布；
- straggler rank/group pattern；
- pfind files checked per process；
- job-stealing time、active utilization/waiting time；
- file system、interconnect、node/process count 等环境字段。

论文明确指出自报 interconnect speed 可能缺 NIC count，只能谨慎作为分类变量；这类字段暂标 `partially-verified`。

### 4.10 TailBench++（`2505.03600.md`）

| 原始指标/字段 | 候选角色 |
|---|---|
| QPS/request rate | workload pressure/input |
| mean latency | workload-objective |
| p95 latency | workload-objective/guardrail |
| p99 latency | workload-objective/guardrail |
| 95% confidence interval | stability/trust |
| client start time | workload context |
| per-client request count | workload context/validity |
| client/server count | topology context |
| per-client QPS schedule | workload pressure |
| load-balancing policy | environment/control variable |

论文展示的是动态多客户端、多服务器场景。它可以证明场景调优能力，但其 QPS/延迟字段不应成为所有组件探针的统一指标。

## 5. Looper 当前 metric 声明（42 条，不去重）

### `benchbase-smallbank`：20 条

`offered_tps`、`attempted_tps`、`offered_requests`、`started_requests`、`completed_requests`、`offered_load_achieved_ratio`、`rate_limiter_lag_ratio`、`client_headroom_ratio`、`committed_tps`、`committed_transactions`、`timeout_count`、`timeout_ratio`、`latency_p50_ms`、`latency_p95_ms`、`latency_p99_ms`、`latency_p999_ms`、`latency_max_ms`、`abort_ratio`、`retry_ratio`、`error_ratio`。

### `config-driven-fixture`：1 条

`fixture_score`。

### `dcperf-mediawiki`：11 条

`closed_loop_successful_rps`、`wrk_rps`、`successful_requests`、`failed_request_ratio`、`error_ratio`、`timeout_count`、`timeout_ratio`、`latency_p50_ms`、`latency_p95_ms`、`latency_p99_ms`、`cpu_utilization_p95`。

### `demo`：5 条

`throughput_mib_s`、`latency_ms`、`compression_ratio`、`roundtrip_ok`、`output_bytes`。

### `sysbench`：5 条

`events_per_sec`、`latency_avg_ms`、`latency_p95_ms`、`latency_max_ms`、`throughput_mib_s`。

同名例子如 `latency_p95_ms`、`error_ratio`、`timeout_ratio`、`throughput_mib_s` 目前保持多行：它们可能具有不同 workload、样本口径和 required 语义，尚未做字段等价性验证。

## 6. Adapter 原始字段（保持独立）

- Atrex synthetic fixture 显式映射：`objective_score`、`elapsed_time`、`correctness`、`stability`。
- DCPerf Benchpress synthetic fixture 显式映射：`throughput`、`latency_p99`、`error_rate`。
- CCL synthetic workload card：`completion_time`、`effective_bandwidth`。
- BenchBase 与 DCPerf MediaWiki scenario adapter 的输入字段很多于最终 benchmark metric；在确定“原始证据字段”和“规范化 metric”的边界前不删除。

## 7. 候选组件树（仅供下一轮讨论）

```text
系统组件指标
├── CPU
│   ├── execution / instruction mix
│   ├── frontend / cache / TLB / branch
│   ├── backend / memory pressure
│   ├── scheduler / affinity / migration
│   └── frequency / power
├── Memory
│   ├── bandwidth / loaded latency / saturation
│   ├── NUMA / locality
│   └── VM / page fault / THP / swap / reclaim
├── Storage
│   ├── bandwidth / IOPS / latency
│   ├── metadata operations
│   └── queue / close / stonewall / straggler
├── Network
│   ├── throughput / PPS / RTT / tail latency
│   ├── loss / retransmission
│   └── IRQ / softirq / placement
├── Accelerator（环境具备时）
│   ├── compute / roofline
│   ├── device memory / host-device transfer
│   └── collective / fabric / overlap
└── 跨组件
    ├── correctness / availability / SLO
    ├── distribution / CV / tail / multimodality
    ├── power / cost
    └── trust / replay / rank stability

workload 场景验收
├── workload primary objective
├── correctness and SLO gates
├── offered-load validity
├── workload-specific tail/goodput
└── 对应系统组件的诊断指标
```

## 8. 尚未确认、不能写入实现的决策

1. CPU 是否继续细分 frontend、backend、cache/TLB、scheduler、frequency/power，还是只保留一级 CPU。
2. NUMA 属于 Memory 还是作为跨 CPU/Memory 的独立组件。
3. accelerator 是否进入首版，还是只保留扩展接口。
4. 通用调优最终是否生成 balanced/throughput/latency/power 多个 profile。
5. 组件探针的 primary objective 如何选择；当前没有证据支持把 IPC、带宽或延迟中的任意一个设为所有机器的统一目标。
6. 同名 Looper metric 的等价规则，以及单位、统计量、scope 的 canonical 化方式。
7. workload 与可调组件的映射是人工声明、诊断后动态选择，还是二者结合。

这些问题确认前，不创建最终 metric schema，不筛除原始字段，也不恢复系统优化器实现。
