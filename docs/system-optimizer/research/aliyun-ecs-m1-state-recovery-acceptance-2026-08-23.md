# 阿里云 ECS M1 配置状态与恢复验收（2026-08-23）

> 证据类型：empirical-alibaba-ecs-kvm
> 代码提交：`a8c88fbcd5c131148c9ccabbfea0e7cc951c35a2`
> 边界：这是 Ubuntu 24.04 / Linux 6.8 KVM guest 事实，不外推到腾讯云 CVM。

## 结果

M1 的配置采集、显式所有权授权、真实人工修改/回滚、过期租约对账、
needs-attention 和人工恢复链在该 ECS 上跑通。最终设备状态恢复为：

- `/sys/block/nvme0n1/queue/scheduler = mq-deadline`
- `/sys/block/nvme0n1/queue/nomerges = 0`
- 原有 Looper worker PID 51193 在验收后仍运行；没有停止或销毁实例。

## 全量采集与 20 项口径

先对 `/proc/sys`、CPU、THP、block、net 和 IRQ 显式根做宽范围采集，再选择目标 selector：

- 原始记录 2,274 条；`enumeration_complete=true`，`all_values_readable=false`。
- selector 全量查看后选择实际 `nvme0n1`、`eth0`、活跃 NVMe IRQ 44 和 CPU policy0。
- 20 项计数仍为 `6 × 3 + IRQ + MTU`，不去重：18 succeeded、2 unavailable。
- unavailable 是 `numa_balancing_scan_period_min_ms/max_ms`；没有补默认值。

安全交付例外：原始读取包含一条 per-host `tcp_fastopen_key`。Git 版证据仅对该记录移除
长度、内容、base64 和内容哈希并标 `unavailable`，其余 2,273 条不变；规则与范围记录在
`redaction-log.json`。未脱敏副本只保留在已授权 ECS，不进入仓库。

配置来源另对 sysctl.d、tuned 等显式根全量读取：73 个 source、497 个可解析
assignment。由于包含所有安装但未必 active 的 tuned profile，保守结果为：

- persistence：5 conflict、1 declared、14 unknown。
- ownership：5 conflict、1 external-writer、14 unknown。

这证明采集器没有把候选 profile 静默当作当前最终优先级；自动写入会 fail-closed。
最终 active profile/继承/发行版优先级解析仍是后续功能，不在本轮臆测。

## 人工修改与恢复

操作者只对 scheduler/nomerges 两项把运行时写所有权显式授予
`m1-optimizer-validation`。随后真实执行 `nomerges: 0 → 1 → 0`：apply、读回 verify、
rollback、再次 verify 全部 succeeded，事务状态 `rolled_back`，snapshot 和 final 都为 0。

## 崩溃对账

演练构造一份已过期租约，代表上个 writer 未释放租约：

1. 初版对账错误读取整个 manifest，而历史快照只含实际修改项，安全地进入
   needs-attention；没有接管。这暴露并修复了快照集合语义缺口。
2. operator-approved snapshot 与现场 `nomerges=0` 一致后清除 attention。
3. 修复后严格按 expected snapshot item 集合复读，actual/expected digest 相等，得到
   `matched-snapshot`。
4. 新 writer 携带绑定原租约的 reconciliation evidence 成功接管并完成事务。

本演练没有 kill 真实优化器进程，因此证明的是“遗留过期租约恢复”，不是进程信号级
crash 注入。

## rollback failure 演练

单项 failure manifest 使用确定返回失败的 rollback command：

- apply `nomerges=1` 与 verify succeeded。
- rollback 到 snapshot 0 failed；失败当时现场值为 1。
- 事务进入 `needs-attention`，后续 writer 被 attention 文件阻断。
- 操作者按 snapshot 恢复 0；`recover-attention` 现场复读 actual/approved 完整一致后
  清除阻断。

该失败与恢复均保留原始事件序列，不能把最终恢复成功改写成“rollback 当时成功”。

## 测试

- System Optimizer：72/72，独立 `--basetemp`。
- 仓库：排除已知 cloud confirmation token flaky 后 292/292，独立 `--basetemp`。
- 未排除共收集 293；不能报告 293/293。
- flaky 独立串行复跑 10 次为 5 pass / 5 fail，旧/新 token 相同；因此已排除“只在共享
  pytest 临时根 + 多 agent 并发才失败”的旧定性。该云购买模块未在本 M1 变更中修改。

## 证据

本地目录：`.artifacts/system-opt/m1-state-ownership-recovery-20260823/`。
机器可读总览见 `acceptance-summary.json`；原始 inventory、状态 evidence、两次
reconciliation、人工事务、failure manifest、failure result 和 recovery 均独立保留。
