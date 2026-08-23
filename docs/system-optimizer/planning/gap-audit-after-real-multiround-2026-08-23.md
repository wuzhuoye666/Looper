# 真实多轮实测后的计划差距审计（2026-08-23）

> 状态：delivery gap audit
> 基准：原开发提示词 M1–M5、新路线图及 2026-08-23 阿里云 ECS 五候选实测。

## 总结

当前已经从“simulated 多轮 + 真实单候选”推进到“真实 Linux 五候选多轮”。这证明候选生成、周期基线、共享预算、安全施加/回滚和无改进停止能够在一台真实 Linux guest 上串通，但完成度仍属于纵向切片，不是原 M1–M5 完整交付。

## 按原里程碑逐项对照

| 里程碑 | 已经满足 | 仍有差距 | 当前定性 |
|---|---|---|---|
| M1 配置层 | Config Manifest、动态域、授权域、安全链、租约；阿里云 ECS 对 scheduler/nomerges 完成真实 apply/verify/rollback；逐项持久化/ownership 状态证据、显式 actor 授权及完整快照崩溃对账已实现 | 腾讯云 CVM 未测；20 项只完成候选目录而非逐项目标验证；跨配置系统的最终优先级不推断；真实 crash/rollback failure 演练未完成；本轮全量回归待完成 | 部分完成 |
| M2 通用调优 | 真实存储组件完成 5 候选、3 基线、8 attempts；预算和停止条件生效 | CPU、内存/NUMA、网络尚无真实组件协议；没有跨组件组合复验、通用 Profile、人可读报告；五个候选均无显著收益 | 存储纵向切片跑通 |
| M3 workload 调优 | synthetic 组件路由、二维优先级和 workload 闭环存在 | 没有真实业务 workload；L0/L1 低开销采集、L2/L3 触发下钻、开销 A/B、干预后业务复验均未实现 | 未验收 |
| M4 平台集成 | Typer CLI 和 local-linux backend 在 ECS 真实执行 | 本次是部署后本机执行，不是 ssh-remote backend 验收；HTTP API、事件流、权限审批、EnvironmentSnapshot schema 双写、UI 和多节点协议未完成 | CLI 切片 |
| M5 交付 | simulated、WSL2、单候选和五候选实录；本地和云端 283 测试全绿 | raw→candidate 绑定缺失；无离线 replay/证据验证器、完整 failure drill、迁移说明、schema 稳定承诺和三命令最终用户验收 | 未完成 |

## 优先级最高的实现问题

### P0：证据不能完整重放

候选只保存 measurement digest，没有保存完整候选 MeasurementBatch、raw artifact digest 列表与安全事件。raw 文件名也没有携带完整配置身份。必须新增 append-only attempt/measurement ledger：候选参数 digest、apply/verify/rollback 事件、配置窗口前后 digest、raw 文件 digest 和 DerivedMetric 输入摘要直接相互引用。

### P0：测量隔离仍靠人工 preflight

引擎没有自动检测 tuned/其他调优器，也没有在测量窗口前后计算完整配置 digest。本次由操作者人工读取 tuned profile，并排除其声明参数；这不能替代 P4 自动隔离合同。环境身份目前只要求 target/workload/phase/tool/statistics，未强制绑定内核、发行版、配置和工具清单完整指纹。

### P0：稳健层没有完整落实

当前自动接受主要由主目标 LCB 决定。P99 是软目标，代码没有实现 `mean_better_tail_worse` 的显式人工决策状态，也没有把稳定性边界作为不可静默越过的门。本次候选都因主目标 LCB≤0 被拒，所以没有触发错误接受，但代码路径仍存在缺口。

### P1：MDE 尚未由噪声校准

本次沿用已确认协议中的 `minimum_effect=0`。虽然三个周期基线很稳定，仍没有把业务最小收益与基线噪声推导成任务 MDE。正式验收前要生成校准证据并由用户确认，不能把 0 固化成跨任务默认值。

### P1：组件覆盖和 workload 下钻不足

真实数据只覆盖存储随机读。需要分别建立 CPU、内存/NUMA、网络协议，再做跨组件退化门与组合复验。workload 路线还需要业务合同、L0/L1 常驻低开销指标、L2/L3 触发采集、组件路由和回到原业务 workload 的因果复验。

### P1：部署包没有可复现清单

云端第一次缺 `benchmarks`，第二次缺 `third_party`，说明当前手工 tar 部署不是可交付安装方式。需要由构建系统生成包含代码、schemas、benchmarks、adapters、third_party lock 和测试清单的带 digest bundle，并在运行前验证完整性。

## 推荐的后续开发顺序

1. 先修证据 ledger、raw 绑定和自动配置漂移检测，否则增加更多实测只会产生更多不可完整重放的数据。
2. 补稳健层：P99/稳定性边界、`mean_better_tail_worse` 状态和人工决策出口。
3. 固化可复现部署 bundle，关闭两次云端缺目录问题。
4. 完成 M2 的 CPU、内存/NUMA、网络组件协议和组合复验。
5. 再进入 M3 真实 workload 的分层观测与下钻。
6. 最后完成 M4 API/事件/权限和 M5 replay、failure drill、迁移及腾讯云 CVM 最终验收。

缓存、快速探针和中间结果复用仍属于 M6，当前不得抢在上述证据与功能闭环之前。
