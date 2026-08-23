# 安全执行、并发与恢复

> 状态：normative draft；补偿事务、默认 simulated 和 fail closed 原则继承；并发细节 open。

## 单目标安全流程

preflight → snapshot → apply → verify → measure → rollback-or-explicit-keep

MUST：

- preflight 检查权限、目标能力、配置前置条件、依赖、互斥、风险、所有权和任务授权域。
- snapshot 保存实际旧值，不使用清单作者 default 代替。
- 每项 apply 后读回；全部 verify 通过后才允许测量。
- apply 是可补偿事务，不宣称多个内核接口同时原子提交。
- 任何已开始并可能部分生效的当前项都必须进入补偿考虑，不能只回滚先前完全成功的项。
- rollback 按依赖逆序并再次 verify。
- rollback 失败或目标状态无法确定时进入 needs-attention，禁止继续搜索。

## 已关闭的 A 级实现问题

1. ConfigItem.preconditions 已接入 backend preflight，并有不满足前置条件即拒绝的专项测试。
2. 当前 apply 项一旦开始，不论返回 failed、timeout 或 unknown 都进入补偿集合，并有部分施加后回滚的专项测试。

这两项的代码缺口已关闭；真实 CVM rollback failure 演练仍是独立验收门，不能用 simulated
专项测试替代。

## 并发

推荐方向是同一目标单写者租约：

- 修改配置的人工任务、通用调优和场景调优互斥。
- 只读采集可并发，但必须检测是否影响测量或与外部 profiler 冲突。
- 外部配置管理器、tuning daemon 或人工 shell 修改造成 drift 时，本轮测量失效并停止写入。

租约 TTL、续约、人工抢占和外部管理器协调尚未确认，不能提前实现默认优先级。

## 崩溃恢复

恢复 MUST 先重新读取目标事实，并与以下内容对账：

- 最近成功快照。
- 已发出的 apply 事件。
- 读回 verify 事件。
- 当前真实配置。
- 任务租约和操作者意图。

对账后才能选择继续、补偿回滚或 needs-attention。不得在 unknown 状态盲目重试 apply。

M1 实现合同进一步规定：

- 过期租约不能用任意 SHA-256 字符串接管；reconciliation 必须内嵌完整 actual/expected
  `ConfigSnapshot` 并绑定原租约 digest。
- `matched-snapshot` 只接受同一 target 的两个完整且 digest 相等的快照。
- CLI 必须现场重新读取 actual snapshot；不完整或不一致时写入 needs-attention，并禁止接管。
- 清除 needs-attention 必须再次现场读回，并提交绑定原 attention evidence 的完整
  actual/approved 快照；不一致时保持阻断。

## 测量隔离

必须记录并控制：

- workload 数据和缓存重置协议。
- 冷/热 cache state。
- 候选顺序、预热和稳态。
- 前一个候选对后一个候选的残留影响。
- profiler、PMU multiplexing 和采集开销。
- 配置、环境和外部进程 drift。

这些测量状态与未来 optimizer evidence cache 是不同概念。

## 多节点

多节点正式协议尚未确认。进入实现前至少定义：节点角色、全体快照、施加顺序、节点失联、部分成功、全局补偿和恢复责任。在此之前第一阶段只验收单目标安全闭环，多节点不得被 README 暗示为已支持。
