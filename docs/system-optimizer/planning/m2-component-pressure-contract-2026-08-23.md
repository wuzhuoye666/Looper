# M2 组件与标准压力合同

> 状态：M2 implementation baseline，2026-08-23
> 适用范围：离线或受控 Linux guest 内的有限闭环调优。
> 环境边界：本轮实测为阿里云 ECS KVM guest，不是腾讯云 CVM 证据。

## 1. 组件计数口径

“当前组件”有两个不同问题，必须分开回答：

| 口径 | 数量 | 成员 | 说明 |
|---|---:|---|---|
| M2 标准压力性能组件 | 5 | CPU、Memory、NUMA、Storage、Network | 路线图要求分别建立压力、指标和验收协议 |
| `ConfigComponent` 代码枚举 | 8 | 上述 5 项 + Scheduler、Stability、Other | 后三项用于跨组件归属或保底分类，不在 M2 单独建立压力套件 |

IRQ、THP、sysctl、cpufreq、I/O queue、MTU 是配置接口或配置类别，不重复计算为
顶层性能组件。Accelerator 保留在指标研究树中，但不属于当前 Linux CVM guest 的 M2
五组件出口。

## 2. 每个组件的最小协议

每个标准压力协议必须显式声明：

1. 目标作用域与不可外推边界；
2. 工具和所有输入参数；
3. `prepare → warmup → measure → 可选 verify/cooldown → cleanup` 阶段；
4. 主指标、辅助指标、硬门禁和原始证据；
5. 重复次数和稳定性统计量；
6. 稳定性状态：首次校准为 `report-only`，批准阈值后为 `hard-gate`；
7. 候选、尝试、时间、连续无改善和目标停止条件；
8. 目标租约与 cleanup 失败的 fail-closed 行为。

`report-only` 禁止携带 `acceptance_limit`；`hard-gate` 必须携带显式阈值。正式候选
闭环只接受 `hard-gate` 协议。该分支防止首次观测 CV 被自动写成通用阈值。

## 3. 当前五组件状态

| 组件 | 当前探针/证据 | 当前状态 | 仍缺 |
|---|---|---|---|
| CPU | stress-ng 固定 matrixprod，8 worker，bogo ops/s | tuned 所有权交接完成（stop 后 governor=schedutil 即真实基线）；schedutil 基线重校准 CV gate；governor 候选闭环完成：powersave 批次 CV 超门禁被拒、performance LCB95≤0 未采纳，全部回滚 | 换 workload/环境必须重校准；powersave 波动来源未归因；不能宣称 governor 优化收益 |
| Memory | sysbench global sequential write，带宽与事件 p95 | target-local gate 生效；THP 两候选真实闭环，无接受候选 | 组合复验；换 workload/环境必须重校准；不是 MESS loaded latency |
| NUMA | numactl 拓扑与绑定探针 | 两台可用 ECS（8C 与 24C）均单 NUMA node，unavailable 有双机证据 | 至少 2 个 NUMA node 的目标机；本地/远端绑定和候选复验 |
| Storage | fio 4K direct randread，多轮 scheduler/nomerges 实录 | 真实多轮已完成 | 与其他组件组合复验 |
| Network | iperf3 两流 loopback，吞吐与重传 | 仅协议栈稳定出数；loopback-only CV gate | 第二台受控 peer 于 2026-08-23 会话中途失联（实例不可达），真实网络候选闭环仍开放；NIC/VPC/RTT/loss 证据 |

## 4. 指标来源边界

- VGO 支持使用 CV 和完整分布判断稳定性；它没有给出本项目统一 CV 阈值。
- SPEC CPU2026 和 DCPerf 支持 CPU/内存微指标树，但 stress-ng bogo ops/s 是本项目
  组件探针输出，不能写成论文原始指标。
- MESS 支持带宽—loaded latency 曲线和饱和分析；sysbench 的事件延迟不是 MESS
  loaded latency，不能做字段等价映射。
- loopback iperf3 不经过 virtio NIC/VPC 路径；它只能验证 TCP 协议栈压力和脚本能力。

## 5. 组合与总体评分

单组件 best observed 不能相加。M2 的通用结果仍保留
`(CPU, Memory, NUMA, Storage, Network, Stability, CrossRegression)` 向量，先过硬门禁，
再做 Pareto 与显式决胜。组件组合后必须在混合压力下重测，才能生成通用 Profile。

内存 THP 已完成一次合法目标机候选搜索，但两个候选均未接受；结论是“安全闭环跑通且
本协议未发现优化效果”，不能写成 `best observed` 或 `validated`。CPU、NUMA 和真实网络
仍没有合法候选搜索；各组件结果不能相加。详见
[THP 候选闭环实录](../research/aliyun-ecs-m2-memory-thp-closed-loop-2026-08-23.md)。
