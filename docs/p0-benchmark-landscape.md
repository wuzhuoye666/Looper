# P0 Benchmark Landscape and Provisional Top 10

状态：P0 初版已于 2026-08-21 对齐；TencentBench 仍为未知内部基线  
证据截止：2026-08-21  
评分规范：`docs/p0-benchmark-evaluation.md`  
完整 23 项审计附录：`docs/p0-candidate-audit.md`  
已批准的本地 Stage 0：`docs/cpu-pilot-design.md`、`docs/stage0-acceptance.md`

## 1. 结论摘要

当前公开证据无法唯一识别会议中的 `TencentBench` / `TenBench`，因此不能声称已经完成对它的正式缺口分析。下表的“补位作用”暂时相对于一个常见的 CPU、内存、磁盘和网络基础测试集合评估；取得内部链接、截图、命令或测试项后必须重评。

初版 Top 10 不是十个立即集成项：

- 8 项进入 `adopt-pilot` 或 `adopt-after-cost-check` 轨道。
- TailBench 系列因许可证、旧依赖和复现风险进入 `research-priority`。
- Atrex-Bench 因产品问题错位进入 `research-priority`，可作为 Agent workload 素材，不能直接包装成服务器 Agent Runtime Benchmark。
- AgentBench 不进入 Top 10。它主要比较 Agent/模型能力，不测服务器在受控并发下的吞吐、尾延迟和资源效率。
- GPU 超节点由 SuperBench 负责场景化验证，NCCL Tests 作为归因微基准；二者不能互相替代。

## 2. 初版 Top 10

评分向量顺序为 `可用性 / 用户价值 / 完备性与补位 / 公平性 / 区分度`。`5` 代表当前规范中的最高证据等级；在 Looper 尚未完成复现前，候选的可用性最高只能记 `4`。

| 优先级 | Benchmark / 论文 | 服务器选型问题 | 五维向量 | 鲁棒性 | 当前轨道 | 主要证据与未决项 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | MLPerf Inference v6.1 / *MLPerf Inference Benchmark* | 哪类 CPU/GPU 服务器能在准确率门槛下满足离线、服务、单流、多流和现代 GenAI 推理 SLO？ | `4/5/5/4/4` | `4` | adopt-after-cost-check | Apache-2.0 代码、正式规则和提交体系；2026 版含 LLM、RAG、视频生成和 Edge Agentic。模型、数据和完整运行成本需逐项核验。 |
| 2 | MLPerf Storage / *Characterizing I/O in Machine Learning with MLPerf Storage* | 哪类存储系统能持续供给训练、checkpoint 和 KV-cache workload？ | `4/5/5/4/4` | `4` | adopt-after-cost-check | Apache-2.0 代码；当前主线含 closed/open 规则、提交校验、Training I/O、Checkpointing、KV-cache、Vector Database，以及文件和对象存储。需核验正式版本和最小可用数据规模。 |
| 3 | MLPerf Training v6.1 / *MLPerf Training Benchmark* | 哪类 AI 服务器以最低时间和成本达到规定模型质量？ | `4/5/5/4/4` | `4` | adopt-after-cost-check | Apache-2.0 代码、time-to-quality 和正式提交规则；当前 workload 已覆盖现代 LLM/MoE/生成模型。参考实现明确不是优化结果，正式对比成本很高。 |
| 4 | SuperBench / *Improving Cloud AI Infrastructure Reliability with Proactive Validation* | 哪种 GPU 服务器或超节点拓扑能稳定通过计算、内存、链路和集体通信验证？ | `4/4/5/4/5` | `4` | adopt-pilot | MIT；USENIX ATC 2024；面向大规模 AI 基础设施主动验证。需把微基准异常映射到用户训练/推理影响。 |
| 5 | DCPerf / *DCPerf: An Open-Source, Battle-Tested Performance Benchmark Suite for Datacenter Workloads* | 哪类通用服务器能更好承载现代 hyperscale 服务，而不是只赢一个综合分？ | `3/5/4/4/4` | `4` | adopt-pilot | MIT；ISCA 2025；支持 x86_64/aarch64；六个生产启发 workload，包含 MediaWiki、FeedSim、TaoBench、SparkBench、DjangoBench 和 VideoTranscodeBench。源码已锁定，但 Looper 尚未完成真实 upstream 运行。 |
| 6 | BenchBase / *OLTP-Bench: An Extensible Testbed for Benchmarking Relational Databases* | 哪类服务器/实例能在指定数据库、事务混合和尾延迟目标下提供更高事务容量？ | `4/5/4/4/4` | `4` | adopt-pilot | Apache-2.0；多 DBMS JDBC 框架，含 TPC-C、TPC-H、YCSB、Wikipedia、Twitter 等；支持变量速率、事务混合及 latency/throughput 日志。需固定数据库配置和持久化层。 |
| 7 | SeBS + SeBS-Flow / *Benchmarking Serverless Cloud Function Workflows* | 哪个平台或承载服务器能更好处理冷启动、突发请求和多函数工作流？ | `4/4/5/4/4` | `4` | adopt-pilot | BSD-3-Clause；EuroSys 2021/2025；支持多语言、AWS/Azure/GCP/OpenWhisk/本地 Docker。托管平台结果不能直接归因到服务器硬件。 |
| 8 | DeathStarBench / *An Open-Source Benchmark Suite for Microservices and Their Hardware-Software Implications for Cloud & Edge Systems* | 哪类服务器能在真实微服务拓扑下满足端到端吞吐和尾延迟 SLO？ | `3/5/5/3/4` | `2` | adopt-pilot | GPL-2.0；ASPLOS 2019；公开 Social Network、Media Service、Hotel Reservation。部署复杂、组件版本和客户端隔离必须重新固化。 |
| 9 | TailBench v0.9 + TailBench++ candidate / *TailBench: A Benchmark Suite and Evaluation Methodology for Latency-Critical Applications* | 哪类服务器在变化请求率下仍能保持 latency-critical 服务的尾延迟？ | `2/5/5/3/4` | `1` | research-priority | IISWC 2016；原包与约 10 GB 输入仍可下载。TailBench++ 增加不定客户端、网络模式和动态 QPS，但无明确许可证，主分支偏 Ubuntu 18，Ubuntu 24 分支仅报告 7/8 workload 可编译。 |
| 10 | Atrex-Bench / *Are LLM-Generated GPU Kernels Production-Ready? A Trace-Driven Benchmark and Optimization Agent* | Agent 生成 GPU kernel 的能力和效率如何受任务、模型与 GPU 平台影响？ | `3/3/5/4/3` | `4` | research-priority | Apache-2.0；2026 早期项目；30 个生产 trace 派生算子，覆盖 AMD/NVIDIA，含 correctness 与 speed-of-light 归一化。它比较 Agent/模型生成 kernel 的质量，不直接回答服务器并发 Agent 容量。 |

这里的顺序首先保障场景覆盖和会议指定的新方向，然后才参考五维证据。它不是把五维相加后的排行榜。

## 3. Top 10 的用户价值

### 3.1 通用服务器与线上服务

- DCPerf 回答现代数据中心应用的整机选择问题。
- DeathStarBench 暴露微服务拓扑、RPC、共享服务和尾延迟问题。
- TailBench 系列提供可控请求率与 latency-critical 方法，但当前不能直接集成。

三者有重叠，但证据层次不同：DCPerf 偏生产启发的整机 workload，DeathStarBench 偏多服务端到端拓扑，TailBench 偏尾延迟方法。P1 必须用任务支配度分析防止同一 Web/RPC 模式被重复计权。

### 3.2 数据、数据库与存储

- BenchBase 把数据库事务混合、速率和延迟带入选型。
- MLPerf Storage 覆盖 AI 数据管道、checkpoint 和 KV-cache，这些不是 `fio` 单独能回答的问题。

`fio`、STREAM、iperf3 和 NCCL Tests 仍有价值，但应作为解释场景结果的 attribution probes，不应成为面向采购用户的综合结论。

### 3.3 AI 服务器与 GPU 超节点

- MLPerf Inference 负责准确率受控的推理性能。
- MLPerf Training 负责 time-to-quality 和规模化训练。
- SuperBench 负责组件、链路、集体通信和可靠性验证。
- NCCL Tests 负责 SuperBench 或训练结果中的 GPU fabric 归因。

这四层分别回答业务性能、训练效率、基础设施健康和链路机制，不能合成一个不透明的 GPU 总分。

### 3.4 Agent Runtime

公开候选仍存在明确空白：尚未找到同时满足以下条件的成熟 Benchmark：

- 固定 Agent harness、模型、工具和任务集；
- 在同一服务器上逐级提高并发 Agent 数；
- 测量完成任务数/秒、成功率、p95/p99 wall time、token 与工具等待分解、CPU/内存/GPU/磁盘/网络每任务资源；
- 处理缓存命中、API 限流、外部服务抖动、重试和长尾；
- 对至少两个服务器目标给出受控区分和硬件归因。

已发现的三个近邻只能作为组成材料：

- MLPerf Inference v6.1 Edge Agentic：有 BFCL v4 单轮准确率门槛和录制式 Agent 编码轨迹单流回放，但不是多 Agent runtime 容量测试。
- AgentBench：有多环境任务和成功率，但核心对象是 Agent/模型能力，外部 LLM/API 会掩盖服务器差异。
- Atrex-Bench：有生产 trace 派生 GPU kernel 任务、正确性和性能归一化，但核心对象是 Agent 生成代码的质量。

P0 结论应写成“需要设计 Looper Agent Runtime scenario”，而不是把任一近邻更名后宣称缺口已填。

## 4. 候选池与未入选原因

本节是摘要；精确 revision、论文、许可证/unknown、逐维评分证据、鲁棒性和 Top 10 diff 见 `docs/p0-candidate-audit.md`。

| 候选 | 主要价值 | 未进入初版 Top 10 的原因 | 后续角色 |
| --- | --- | --- | --- |
| CloudSuite 4.0 | 八个真实软件栈，含 ARM，覆盖 analytics、cache、data serving、graph、media、search、Web | 与 DCPerf/DeathStarBench 高度重叠；组件许可证需逐项处理 | DCPerf 架构兼容性不足时的强备选 |
| PerfKit Benchmarker | 跨云自动建机、默认不做厂商特调、治理规则成熟 | 更像云测试编排和适配层，与 Looper 控制面重叠 | 公平性和 provider adapter 的方法参考 |
| NCCL Tests | correctness、collective bandwidth、单/多节点、per-iteration p99 和 JSON 原始结果 | 是 NVIDIA/NCCL 归因微基准，不是完整用户场景 | SuperBench 与训练场景的必选 companion probe |
| IO500 | IOR、mdtest、find、正式规则、结果验证和公开榜单 | 面向 HPC 并行文件系统，普通 CVM 本地/云盘场景匹配度较低 | 有 HPC/共享存储用户时升级 |
| SHARP | 可重复异构/FaaS 实验、统计与 profiling；CPU、CUDA、MPI、Fission、Knative | 主要是框架和合成 workload，采购用户价值低于 SeBS/SuperBench | Looper 证据与变异性方法参考 |
| AgentBench | Agent 多环境任务能力和成功率 | 模型/Agent 能力基准，不是服务器性能基准 | Agent Runtime 的任务与正确性素材 |
| YCSB | 键值/云数据库 workload 通用 | BenchBase 已包含 YCSB，并提供更广的 DB workload | BenchBase 内部场景或独立轻量适配器 |
| fio / STREAM / iperf3 / HPCG | 存储、内存、网络和计算归因清晰 | 单机制微基准，用户价值不足以独立进入场景 Top 10 | 每个场景报告的解释层 |
| SPEC CPU2026 | 规则严格、CPU 区分度高 | 商业许可、不可再分发、场景覆盖窄 | 可选外部证据，不进入默认开源套件 |
| GenAI-Perf / LLMPerf | LLM 请求生成、吞吐和延迟工具 | 工具边界、当前仓库身份和结果规则仍待复核；与 MLPerf Inference 重叠 | 轻量在线 LLM smoke test 候选 |

## 5. GitHub 与上游同步快照

以下 revision 通过 GitHub commit Atom feed 或已有锁文件在证据截止日解析。它们是研究快照，不代表已经完成许可证审批和本地复现。

| Upstream | 默认分支快照 | 最近活动时间 UTC | 本地状态 |
| --- | --- | --- | --- |
| facebookresearch/DCPerf | `9308c3e3c404e0466f0a2929f15ddcf62b2215f6` | 2025-09-09 | archive/许可证已校验；normalizer 已集成，upstream workload 尚未运行 |
| mlcommons/inference | `b66003e10e2db4b8c36a448b0545ec90fcc6e9e9` | 2026-08-20 | 未锁定 |
| mlcommons/training | `b8b3adcaa0db110a79dc19e183c22b54489b48fa` | 2026-08-17 | 未锁定 |
| mlcommons/storage | `ce28a9888076eba5a58fdd93d1c0258bf27b7aa2` | 2026-08-14 | 未锁定 |
| microsoft/superbenchmark | `67298aef5decc64e0f67877c8916898bebc87847` | 2026-06-03 | 未锁定 |
| cmu-db/benchbase | `33c00473807ebd49304d114a6d769d2d2b2bbb34` | 2025-12-13 | 43,099,345-byte archive/许可证已校验；normalizer 已集成，upstream workload 尚未运行 |
| spcl/serverless-benchmarks | `ef76f4278c6c7eaf1779f661bc26d5c79c7e4330` | 2026-08-18 | 未锁定 |
| delimitrou/DeathStarBench | `6ecb09706140f8730b5385c08f1386c654c3c526` | 2024-06-27 | 未锁定 |
| zliUPV/Tailbenchplusplus | `1a707726ddd171ebf7e2bd3db52f5f5bbe4a9c7c` | 2026-01-27 | 仅目录记录；许可证阻塞 |
| alibaba/atrex-bench | `e09242e96b73b22d20a0411099947558e1861b4e` | 2026-08-20 | 已下载并校验，尚未真实集成 |
| NVIDIA/nccl-tests | `717b68318278e93f371d8ffb46b076069d7c7851` | 2026-08-03 | 未锁定 |
| GoogleCloudPlatform/PerfKitBenchmarker | `946ea317692cac6c78e4aec1cd041b538f1a0285` | 2026-08-21 | 未锁定 |
| parsa-epfl/cloudsuite | `c9d7584b9f4f0dec56e6683ebd61dad66ac1d06a` | 2023-06-25 | 未锁定 |
| THUDM/AgentBench | `d1e4a10db08c87075c78972e48ecc182be03e2d5` | 2026-02-08 | 未锁定；仅作为素材候选 |
| HewlettPackard/SHARP | `4dc3f73c28afb89efb653b989f41cd5955c6f91e` | 2026-04-27 | 本地仍锁 `v2.0.0` / `e8dd8b5...`，需评估升级而非静默替换 |

注意：分支快照会移动。只有经许可证复核后写入 `third_party/sources.lock.yaml` 并验证归档摘要的 revision 才属于 Looper 受控供应链。

## 6. 论文与公开来源

1. MLPerf Inference Benchmark: https://arxiv.org/abs/1911.02549
2. MLPerf Training Benchmark: https://arxiv.org/abs/1910.01500
3. SuperBench: https://arxiv.org/abs/2402.06194
4. DCPerf: https://doi.org/10.1145/3695053.3731411
5. DeathStarBench: https://doi.org/10.1145/3297858.3304013
6. TailBench: https://doi.org/10.1109/IISWC.2016.7581261
7. SeBS: https://arxiv.org/abs/2012.14132
8. SeBS-Flow: https://arxiv.org/abs/2410.03480
9. OLTP-Bench / BenchBase: http://www.vldb.org/pvldb/vol7/p277-difallah.pdf
10. MLPerf Storage paper: https://doi.org/10.1145/3572751.3572765
11. MLPerf Storage repository: https://github.com/mlcommons/storage
12. Atrex-Bench: https://arxiv.org/abs/2607.14541
13. SuperBench repository: https://github.com/microsoft/superbenchmark
14. MLPerf Inference Edge Agentic: https://github.com/mlcommons/inference/tree/master/language/edge-agentic
15. NCCL Tests: https://github.com/NVIDIA/nccl-tests

## 7. P0 剩余阻塞项与下一授权

公开候选池、逐维证据、Top 10、CPU pilot 和本地 Stage 0 已完成并记录。唯一无法由公开检索闭合的 P0 项仍是实际 `TencentBench` / `TenBench` 基线：需要内部 URL、附件、截图、命令、安装包名、输出表头，或会议原句与上下文中的任一可信标识。取得前不能发布“相对 TencentBench 的确认缺口”。

P1 当前仍有两道独立授权边界：

1. Stage 1 本地工程：构建固定 digest 的 BenchBase/PostgreSQL 与 DCPerf runtime image、上游 launcher 和 container/remote executor，并让 scheduler 消费真实 frontier evidence。
2. Stage A 腾讯云可用性：只有 Stage 1 本地证据通过后，才能在明确的资源清单、地域、最长存活时间和不超过 `CNY 25` 的预算授权下创建资源；Stage B/C 仍需再次授权。

当前 Top 10 成员和顺序没有因审计附录而变化。Agent Runtime 仍需设计独立 Looper scenario；AgentBench 与 Atrex-Bench 只能提供任务/正确性素材。
