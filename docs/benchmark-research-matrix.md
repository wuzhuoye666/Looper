# Benchmark 接入形态调研矩阵

调研日期：2026-08-23。本文把论文清单与公开上游证据翻译为 Looper 接口需求；数字仅在上游明确给出时使用，未确认的机型下限不得由接入者猜测。

## 结论

论文清单中的套件并不是一种运行模型。至少要覆盖五类基础设施：单机批处理、客户端/服务端、动态多客户端/多服务端、分布式加速器、并行存储集群；还要支持同一套件跨机器/日期的大量重复审计。因此接口必须把“业务角色、实际机器组、运行输入、证据和审计计划”拆开。

## 调研矩阵

| Benchmark / 研究 | 典型拓扑与规模 | 接入时必须声明的机器事实 | 必需输入与证据 | 对 Looper 的要求 |
| --- | --- | --- | --- | --- |
| 性能优化 Benchmark 可靠性审计 | 同一批 task 在多种机器上重放；论文使用 4 类 GCP 机器 | 被测机器类、日期/placement、运行时与编译器必须成为环境轴 | base/reference patch、任务结果、每 task 分数和原始时间 | `audit.environmentAxes` 覆盖 machine/day；Reference 不能只做网页勾选 |
| CCL-Bench 1.0 | 多节点、多 GPU/TPU；每节点加速器数取决于 workload card | 模型、phase、precision、batch/sequence、每节点设备数、总设备数、TP/DP/PP/EP、通信库和网络 fabric | workload card、外部 profiler trace、启动脚本、trace→metric 映射 | GPU workers/coordinator 机器组；大 trace 用 artifact 引用；拓扑与设备映射必须可追溯 |
| DCPerf | 单机全栈或显式 client/server；套件包含多个服务角色 | 官方支持 x86_64/aarch64、CentOS Stream 8/9 或 Ubuntu 22.04、root、联网安装、`ulimit -n ≥ 65536`；具体 CPU/内存按 workload 再确认 | Benchpress job/roles、system_check、原始 JSON、system specs、perf/profile | root/systemd/perf 等 privilege 可声明；安装网络与测量网络分离；角色不能压成一个标签 |
| VGO / SHARP | 主要是单机 CPU/GPU，但每 case 可重复 500/1000 次 | CPU/GPU 型号、驱动、编译器、功耗/频率控制、重复预算 | 每次原始样本、低层 counters、分布和归因结果 | `audit.minimumRepeats` 上限足够大；seed/driver/runtime 成为环境轴；不要只存均值 |
| CloudyBench | client + 云数据库 compute 节点 + 共享/远端存储 + 故障控制；读写主节点和可选只读节点 | 节点数、租户数、实例/存储规格、网络、扩缩容和故障注入权限 | QPS 波形、租户配置、价格、复制延迟、fail-over、资源成本 | controller 独立机器组；动态负载与 fault-injection privilege；cost/topology 原始证据 |
| Atrex-Bench | 生成与评估分离；评估运行在 AMD ROCm 或 NVIDIA CUDA GPU 容器 | GPU vendor/model/count/显存、驱动与镜像、DSL backend；不同 agent workflow 不可混比 | candidate artifact、参考算子、5 个或更多正确性 case、编译结果、timing、roofline/profile | generation/evaluator 隔离；device 输入；正确性先于性能；GPU fallback 必须硬失败 |
| MESS 2.0 / Ramulator 2.0 | 真机单节点 CPU/GPU，或 standalone simulator | 架构、NUMA、内存容量/通道、hugepages/权限；模拟器需配置和 trace | 读写比例、带宽—时延曲线、饱和点、模拟器配置、sanity check | single target 或 simulator 角色；曲线原始点是证据；实机与模拟结果不能混为一种环境 |
| SPEC CPU2026 | 单机；SPECspeed 和 SPECrate 资源需求不同 | 官方支持 Arm/Power/RISC-V/x86；SPECspeed 物理内存 64 GiB；SPECrate 每 copy 2 GiB；建议磁盘 1 TB，Rate 另加每 copy 5 GB；编译器标准和许可证 | suite/config、Base/Peak、copies/threads、编译器、官方原始报告 | 许可证/数据集使用边界；speed/rate 分 workload；copies 影响机器下限，不能写死为一个通用值 |
| IO500 | MPI 多客户端访问一个存储系统；存储服务器可独立或与客户端同机 | 客户端数、每节点进程、存储节点/挂载、MPI、网络 fabric、容量、是否破坏；获奖提交至少 10 个物理 client 节点 | INI、config hash、逐阶段/逐进程日志、持久化和 read-after-write 检查 | clients/storage 机器组、device + topology 输入；官方阶段不中断；持久性和 300 秒 stonewall 为硬门禁 |
| TailBench++ | 动态多客户端、多服务器，可在运行中加入客户端并改变 QPS | client/server 数量范围、负载均衡器、端点、网络 RTT、客户端发压能力 | 到达时间、分段 QPS、请求账本、p95/p99、timeout 和 server 映射 | count 必须是范围而非一个数字；动态 topology/config；客户端 headroom 和 SLO-goodput |
| Sysbench（当前已接入） | CPU/memory/thread/mutex 为单机；OLTP 变体为 client/database | 单机模式声明 CPU/内存下限；OLTP 另建 client-server 版本并声明 DB endpoint/secret | 原始文本、事件率、带宽、延迟和执行检查 | 不要用单机微基准 manifest 假装覆盖数据库 OLTP；每种语义独立版本/包 |
| Phoronix Test Suite（PHPBench pilot 已接入） | PTS 是测试框架；具体拓扑由 profile 决定。首个 `pts/phpbench-1.1.6` 为单机 | PTS 10.8.6、PHP CLI、解压工具；PHPBench profile 只需 1 个逻辑 CPU，未从上游确认的内存下限不猜测 | 固定 profile、下载物 SHA-256、PTS 原始 JSON、原始重复分数、系统指纹 | 禁止把 profile ID 设成自由参数；不同单位/方向/依赖的 PTS profile 必须形成独立 Looper 包 |

## 字段覆盖检查

| 调研发现 | 接口字段 |
| --- | --- |
| 机器数不是常量 | `nodeGroups[].count.minimum/default/maximum` |
| client、server、controller、storage、simulator 不同 | `nodeGroups[].role` |
| 机型下限随角色不同 | `nodeGroups[].requirements` |
| GPU 数量/显存/互联 | `requirements.accelerators` |
| 存储容量、介质、共享和破坏性 | `requirements.storage` + `device` input |
| 带宽、RTT、fabric、RDMA | `requirements.network` + `infrastructure.links` |
| 同机/隔离/同可用区 | `nodeGroups[].placement` |
| 外部机器拓扑 | required `topology` input |
| 大数据/trace 不应进 Git | digest-required `artifact` / `dataset` input |
| 多次、多天、多机审计 | `spec.audit` |
| 正确性不可被速度补偿 | `scenario.slo_gates` / `adapter.requiredChecks` |
| 私有输出格式多样 | suite-owned deterministic normalizer |

## 当前实现缺口

1. Looper 已能保存并校验完整机器合同，但 Worker 仍是单入口执行模型。
2. `adapter` 自编排多机可通过 required topology 输入接入；节点准备和远程权限仍由套件/外部系统负责。
3. `looper` 自编排多机器组已经预留枚举，但在调度器实现前必须保持 Stage 0。
4. 当前目标清单对 CPU/内存有指纹数据，对 GPU、网络和存储的统一 inventory 尚不完整；Adapter 必须在运行前再次验机并 fail closed。

## 主要来源

- 本项目论文清单：`02_研究项目/outputs/第一次论文内容.md`
- [可靠性审计论文](https://arxiv.org/abs/2607.01211)
- [CCL-Bench 官方仓库](https://github.com/cornell-sysphotonics/ccl-bench)
- [DCPerf 官方仓库](https://github.com/facebookresearch/DCPerf)
- [VGO 论文](https://icpe2026.spec.org/preprint/Variability-Guided_Performance_Optimization.pdf)
- [CloudyBench 论文](https://dbgroup.cs.tsinghua.edu.cn/ligl/papers/ICDE25-CloudyBench.pdf)
- [Atrex-Bench 官方仓库](https://github.com/alibaba/atrex-bench)
- [MESS 2.0 官方仓库](https://github.com/bsc-mem/Mess-2.0) 与 [Ramulator 2.0](https://github.com/CMU-SAFARI/ramulator2)
- [SPEC CPU2026 系统需求](https://ftp.spec.org/cpu2026/docs/system-requirements.html)
- [IO500 官方规则](https://io500.org/rules/submission) 与 [官方仓库](https://github.com/IO500/io500)
- [TailBench++ 论文](https://arxiv.org/abs/2505.03600)
- [Sysbench 官方仓库](https://github.com/akopytov/sysbench)
- [Phoronix Test Suite 官方仓库](https://github.com/phoronix-test-suite/phoronix-test-suite) 与 [PHPBench profile](https://openbenchmarking.org/test/pts/phpbench)
