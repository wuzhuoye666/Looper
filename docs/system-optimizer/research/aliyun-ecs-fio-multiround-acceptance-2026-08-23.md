# 阿里云 ECS fio 多轮自动闭环实测（2026-08-23）

> 状态：empirical acceptance record
> 适用边界：仅验证本台阿里云 ECS 上的真实多轮控制、安全回滚和停止策略；不是腾讯云 CVM 证据，也没有证明获得正向性能优化。

## 环境与隔离边界

- Alibaba Cloud ECS，Ubuntu 24.04.4，Linux 6.8.0-137-generic，KVM，8 vCPU。
- 根文件系统 `/dev/nvme0n1p3`，ext4；测试文件沿用已准备的 1 GiB 文件。
- 服务器为共享环境，另有空闲 Looper worker 和 root 会话。
- `tuned` 以 `virtual-guest` profile 运行，且 `tuned-adm verify` 在测试前已经失败。
- 本次显式排除 tuned profile 声明的 CPU、readahead 和 sysctl 参数，只搜索其配置未声明的 NVMe scheduler 与 nomerges；该处置降低冲突范围，但不等价于严格隔离环境。

## 显式协议

- workload：fio 3.36，4 KiB randread，direct=1，numjobs=2，iodepth=16。
- 每批：3 秒 ramp + 15 秒 steady，重复 5 次。
- 搜索域：scheduler `{mq-deadline, none}` × nomerges `{0,1,2}`；排除原始基线后共 5 个候选。
- 每 2 个候选刷新一次基线；初始基线、周期基线和候选共享 `max_attempts=8`。
- 95% bootstrap 区间，2000 次重采样，seed 20260823；主目标为 read IOPS。
- 候选只有在主目标改善量置信下界严格大于 0 时才接受；每轮默认回滚。

nomerges 的三个模式来自 Linux 官方 queue sysfs 合同：0 启用全部合并，1 只保留简单 one-hit 合并，2 禁止合并算法。该值域不是根据本次结果倒推出来的。

## 执行结果

- 开始：2026-08-23 11:27:00 +08:00。
- 结束：2026-08-23 11:39:13 +08:00。
- 引擎墙钟：732.302 秒。
- 5 个候选、3 次基线、8 个 measurement attempts。
- 停止原因：`no-improvement-policy`。
- 推荐候选：无。

| 轮次 | scheduler | nomerges | IOPS 点变化 | IOPS 95% LCB | P99 点变化 | 结论 |
|---:|---|---:|---:|---:|---:|---|
| 1 | none | 0 | +0.0354% | -0.0059% | 0.0000% | 拒绝 |
| 2 | mq-deadline | 1 | -0.0069% | -0.0629% | +0.8439% | 拒绝 |
| 3 | none | 1 | +0.0182% | -0.0326% | 0.0000% | 拒绝 |
| 4 | mq-deadline | 2 | +0.0368% | -0.0659% | +1.6736% | 拒绝 |
| 5 | none | 2 | -0.0052% | -0.0978% | 0.0000% | 拒绝 |

第 4 轮只有最高的 IOPS 点估计，但置信下界为负，P99 同时变差，不能称为优化。三个基线 IOPS 中位数为 4196.709299、4196.062571 和 4196.789022，跨度约为初始基线的 0.0173%。

## 安全与完整性核验

- 五个候选全部 feasible，五轮安全状态全部为 `rolled_back`。
- 最终读回：scheduler 为 `mq-deadline`，nomerges 为 `0`。
- 40 份 raw fio JSON、80 个 fio jobs；job error 为 0，零读取字节 job 为 0。
- 测试后没有 fio、stress-ng、iperf3 或 pytest 残留进程。
- 云端完整依赖部署后的最终回归：283/283 通过，使用独立 basetemp。
- 前两次云端回归分别因精简部署包缺 `benchmarks` 和 `third_party` 失败；失败未覆盖，第三次补齐完整测试依赖后全绿。这是部署打包流程缺口，不是静默删除的测试失败。

## 发现的审计阻断项

当前 run 为候选保存了聚合 measurement digest，但 CandidateEvaluation 没有保存完整 MeasurementBatch 或 raw artifact 清单；fio raw 文件名只包含 scheduler，不包含 nomerges。因而可以按时间和执行顺序推断 8 个批次，却不能从合同字段直接证明某五份 raw JSON 属于哪个 nomerges 候选。

该缺口不影响“命令真实执行、五轮安全回滚、统计拒绝”这一结论，但阻止其成为最终的全链路可重放证据。后续必须让测量 adapter 输出 raw artifact digest 列表，并由候选、MeasurementBatch 和配置 digest 显式绑定。

## 证据位置

- 本地：`.artifacts/system-opt/aliyun-ecs-fio-multiround-20260823/`
- 服务器：`/opt/looper-system-opt-evidence/ecs-fio-multiround-20260823/`
- 部署版本：`/opt/looper-system-opt-2cd521e/`
- 服务器归档：`/tmp/looper-system-opt-evidence-ecs-fio-multiround-20260823.tar.gz`

服务器路径均按用户要求保留，未清理、未销毁。
