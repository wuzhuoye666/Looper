# S4-02 目标本地 scale/reference 校准任务包

> 状态：本地合同已实现；真实数值待任务所有者确认并实测。
> 依赖：S4-01 v1 冻结、真实目标授权。
> 禁止：本文不把标准差、IQR、容量、论文数值或任意阈值设为默认。

## 1. 本轮实现边界

`priority_calibration.py` 新增内容寻址的 `S4ScaleCalibrationBundle`。它不是 scale
估计器，而是把已审批的完整 `MetricContract` 与以下身份绑定：

- target id 与 `EnvironmentSnapshot` digest；
- workload contract 与压力协议 digest；
- `F-PROJECT-S4-PIECEWISE-LINEAR/v1alpha1`；
- 形成审批的校准批次、审批证据和审批者；
- 每个 component-diagnostic metric 的 scale、reference、方向、单位和组件。

在线路由消费前必须逐项调用 `verify_s4_scale_calibration()`。策略中的诊断指标与
bundle 必须精确全覆盖，完整 `MetricContract` 必须相等；缺项、多项、unavailable、
身份漂移或 scale/reference 变化均 fail-closed。持久化采用 digest 文件和固定索引，
回放拒绝缺文件、孤儿、畸形文件名和内容篡改。该自包含索引只证明内部完整性，
`approval_evidence_digest` 的真实性仍需外部可信工件或签名确认。

## 2. 当前 metric 事实盘点

当前 synthetic workload policy 有四个 component-diagnostic metric，真实任务不得直接
复用其数值：CPU pressure、memory pressure、network pressure、storage pressure。
NUMA 在当前 demo 中没有独立诊断项。通用 CPU/memory/network/fio policy 的主要指标
是 business-objective 或 hard-gate，也不能因为名称相似就自动映射为 S4 诊断项。

因此真实校准必须先提供最终 workload policy。计数口径是“该 policy 中每个唯一
component-diagnostic metric 一条校准状态”，不是按采样点、collector 或组件数量计数。

## 3. 真实任务必填输入

| 输入 | 必须提供的依据 | 未提供时 |
|---|---|---|
| 目标与环境 | target id、真实 `EnvironmentSnapshot`、采集时间和 digest | 不执行 |
| workload | 完整 `WorkloadContract` 与 digest | 不执行 |
| 压力协议 | 命令、并发、持续时间、采样窗、隔离条件及 digest | 不执行 |
| metric 合同 | id、component、unit、direction、role、scale、reference | 不发布批准项 |
| scale/reference 依据 | 任务显式选择：工程容量、业务限值、同环境经验分布或其它可复核依据 | 记录 unavailable 或待决，不猜值 |
| 采样计划 | 重复次数、稳定性口径、异常样本处理、解释阈值 | 不执行 |
| 授权 | 可读源、允许的负载和配置写集合、审批人 | 只做已授权只读探测 |

选择 scale 推导方法会改变 S4 数值与排序，属于用户决策。目前需要用户逐 metric 确认
推导依据和数值；实现不会在缺少确认时自动计算并批准。

## 4. 四层验收

1. 可获取：逐 metric 保存原始源、单位、读取权限和 unavailable 原因。
2. 可构建：在目标上运行绑定后的 collector/workload，禁止用直读代理冒充完整链路。
3. 稳定出数：按任务批准的重复次数和统计口径保存全部批次；不删除异常样本，单列原因。
4. 区分度：只执行获授权的对照变化，判断该 metric 能否区分状态；无授权则明确未做。

每个通过项最终输出：原始批次、校准分析、审批证据、bundle、固定索引和回放结果。
“可接受/不可接受”只有在任务提供解释阈值后才能裁决。

## 5. 关闭条件

- 实际 workload policy 的每个诊断 metric 都是 approved，或明确 unavailable 并使在线路由
  fail-closed；
- 原始工件能绑定到同一 target/environment/workload/protocol；
- bundle 可离线重算 digest 并通过回放；
- 没有 synthetic、论文或其它环境数值冒充目标实测；
- 用户确认 scale/reference 方法、数值和解释阈值。
