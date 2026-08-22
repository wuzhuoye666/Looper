# TencentBench / TenBench Identity and Gap Report

状态：公开身份待确认；P0 已批准暂按未知内部基线继续  
证据截止：2026-08-21  
适用范围：公开资料，不代表腾讯内部资料

## 1. 严谨结论

公开资料不足以把会议纪要中的 `TencentBench` / `TenBench` 唯一映射到腾讯正式发布的性能 Benchmark、产品、仓库或文档。

当前唯一可辩护的状态是：

`unresolved-internal-baseline`

这意味着：

- 可以报告“未找到公开证据”；
- 可以列出待验证的缺口假设；
- 不能报告“该套件不支持某 workload”；
- 不能推定许可证、测试项、指标、运行方式或维护状态；
- 不能把腾讯云实例规格文档当成可复现 Benchmark 结果。

## 2. 公开检索结果

| 检索对象 | 结果 | 证据状态 | 判断 |
| --- | --- | --- | --- |
| GitHub 仓库精确名称 [`TencentBench`](https://github.com/search?q=TencentBench&type=repositories) / [`TenBench`](https://github.com/search?q=TenBench&type=repositories) | 2026-08-21 repository search 均返回 `total_count: 0` | confirmed-search-result | 未找到公开仓库，但不能证明内部项目不存在 |
| Gitee 仓库精确名称 | 2026-08-21 未发现匹配项目 | public-evidence-absent | 不能确认项目身份 |
| Crossref 学术索引精确名称 | 2026-08-21 未发现可唯一映射的论文或项目 | public-evidence-absent | 不能确认学术项目身份 |
| 腾讯云开发者社区精确查询 `TencentBench` | 2026-08-21 查询返回 `total: 0` | confirmed-search-result | 未找到公开文章，但不能证明内部项目不存在 |
| `third_party/sources.lock.yaml` | 无 TencentBench/TenBench source record | confirmed-local-state | 当前 Looper 没有已治理的该上游 |
| `dsbqaq/benchmark_reproduce_server_tencent` | README 为 San2Patch reproduction workspace，无许可证且非腾讯官方项目 | confirmed-exclusion | 排除 |

检索名称可能是 `TBench`、`Tencent Benchmark`、`10-bench` 或口头简称，名称搜索不能替代会议原始上下文。

## 3. 已排查的近邻

### 3.1 Tencent Cloud CVM 实例规格

官方页面：

- https://cloud.tencent.com/document/product/213/11518
- https://www.tencentcloud.com/document/product/213/11518

它持续发布实例族、CPU 厂商/代际、vCPU/内存组合、本地盘或云盘适配、私网带宽、PPS、收发队列等规格事实。这些信息对候选机型初筛和结果归因有价值。

但公开页面不是一套 Benchmark：

- 没有统一可下载运行器；
- 没有完整 workload、运行参数、预热和重复协议；
- 没有每台目标的原始样本、方差和置信区间；
- 规格上限不能替代用户 workload 的可持续性能。

身份判断：`official-specification-source`，不是已确认的 TencentBench。

### 3.2 TencentOS Server / OpenCloudOS

相关入口：

- https://github.com/Tencent/TencentOS-kernel
- https://www.opencloudos.org/
- https://github.com/OpenCloudOS

它们是操作系统、内核和生态项目。相关材料可能运行 `perf`、SPEC、UnixBench、STREAM、fio、netperf 等工具，但未发现一套公开、统一打包并正式命名为 TencentBench/TenBench 的服务器选型套件。

身份判断：`platform-projects`，不能从其许可证推定未知 Benchmark 的许可证。

### 3.3 TencentOS Tiny / TobudOS

https://github.com/Tencent/TencentOS-tiny

这是 MCU/IoT RTOS 项目。除非会议讨论嵌入式芯片而非服务器采购，否则不属于本项目基线。

身份判断：`out-of-scope`。

### 3.4 CIS Tencent Cloud Computing Benchmark

https://www.cisecurity.org/benchmark/tencent_cloud_computing

这是身份、日志、网络、存储和云配置安全控制的合规基线。指标是控制项通过情况，不是 CPU、内存、存储、网络或 GPU 性能。

身份判断：`security-benchmark-not-performance`。

### 3.5 可能被内部集合调用的标准工具

内部脚本即使调用下列公开工具，也只能证明某个测试组件，不构成 `TencentBench` 身份证据；许可证也必须按组件和版本分别治理，不能继承 TencentOS 内核或外层脚本的许可证。

| 工具 | 官方来源 | 能力边界 |
| --- | --- | --- |
| SPEC CPU | https://www.spec.org/cpu2017/ | 商业许可的 CPU speed/rate；不能代表整机应用、网络或存储 |
| MLPerf | https://mlcommons.org/benchmarks/ | AI 训练/推理等规则化场景；具体代码、模型和数据许可分别核验 |
| Phoronix Test Suite / OpenBenchmarking | https://www.phoronix-test-suite.com/ | GPLv3 测试编排与结果生态；被调用测试各有独立许可和语义 |
| UnixBench | https://github.com/kdlucas/byte-unixbench | GPLv2 传统 Unix 综合分；不提供现代采购 workload 的统一 SLO |
| fio | https://github.com/axboe/fio | GPL-2.0 块存储 IOPS、带宽和延迟；结果依赖完整 IO profile |
| STREAM | https://www.cs.virginia.edu/stream/ | 内存带宽微基准；不能单独代表数据库或应用容量 |
| iperf3 | https://github.com/esnet/iperf | BSD-3-Clause 网络吞吐工具；不能替代多流、包长、抖动和跨 AZ 协议 |

身份判断：`possible-components-only`。只有实际脚本、版本和输出才能证明未知内部集合使用了哪些工具。

## 4. 待验证缺口矩阵

下表的状态不是“确定缺失”，而是“没有公开证据证明未知基线已覆盖”。取得实际 artifact 后逐项改为 `covered`、`partial` 或 `gap`。

| 采购决策能力 | 公开证据状态 | 需要从实际 artifact 核验的内容 |
| --- | --- | --- |
| 真实应用场景 | unknown-until-artifact | 是否只有 CPU/内存/磁盘/网络微基准，还是包含 Web、数据库、缓存、分析和 AI workload |
| SLO 和尾延迟 | unknown-until-artifact | p95/p99/p99.9、timeout、goodput、coordinated omission、最小样本数 |
| 重复与不确定性 | unknown-until-artifact | 预热、重复次数、原始样本、CV、置信区间、异常值规则 |
| 公平性控制 | unknown-until-artifact | 镜像、内核、驱动、编译器、governor/EPP、NUMA、cgroup、功耗和 run order |
| 云黑盒变异 | unknown-until-artifact | 实例重建、宿主代际、超分、steal、邻居噪声、时间块和 AZ |
| 数据库/cache | unknown-until-artifact | 事务混合、数据规模、持久化层、负载生成器隔离和 SLO 搜索 |
| 微服务/RPC | unknown-until-artifact | 多服务拓扑、客户端隔离、网络路径和端到端尾延迟 |
| AI inference | unknown-until-artifact | 模型/数据/准确率门槛、吞吐、首 token/每 token 延迟、功耗 |
| AI training | unknown-until-artifact | time-to-quality、扩展效率、失败恢复和 accelerator utilization |
| GPU supernode | unknown-until-artifact | collective、NVLink/PCIe/RDMA 拓扑、节点内/节点间、链路归因和稳定性 |
| Agent Runtime | unknown-until-artifact | 固定 Agent/模型/工具下的并发容量、成功率、p99 和每任务资源 |
| 成本与能效 | unknown-until-artifact | 实例价格快照、执行时间、每任务成本、功耗、每瓦性能和 TCO |
| 跨供应商可比性 | unknown-until-artifact | 规格归一化、同等 SLO、公开参数和厂商特调披露 |
| 可审计证据 | unknown-until-artifact | source revision、镜像摘要、环境指纹、原始日志、校验和与结果 bundle |

## 5. 如果实际基线只是规格或微基准集合

只有在取得 artifact 并确认这一前提后，才能把下列项升级为“确认缺口”：

- workload 级用户问题与 SLO；
- 可复查的原始结果和统计不确定性；
- 多 placement 的云实例变异；
- 数据库、微服务和现代 AI 场景；
- Agent Runtime 与 GPU 超节点；
- 成本、能效和硬件归因；
- 跨供应商公平比较。

在此之前，它们只是假设。

## 6. 所需的最小识别材料

以下任一材料即可显著缩小身份范围：

1. 内部或公开 URL。
2. 会议附件、截图、Logo 或一页演示文稿。
3. 安装包、镜像、脚本、命令或文件名。
4. 输出表头或指标示例。
5. 版本号和大致使用日期。
6. 会议原句及前后文，或录音时间戳。
7. 明确的平台范围：CVM、裸金属、TencentOS Server、MCU 或 AI 服务器。
8. 提及该名称的讲者、维护团队、公司或业务线。
9. 是否为腾讯内部工具、是否需要内网，以及当前团队是否具备访问权限。
10. 示例输出中的测试项名称、指标字段和单位。

在材料到位前，Looper P0 可以继续评估公共候选，但“相对 TencentBench 的完备性/补位作用”不能最终签字。不得因为公开搜索为空而填造其 workload、许可、活跃度或内部能力缺口。
