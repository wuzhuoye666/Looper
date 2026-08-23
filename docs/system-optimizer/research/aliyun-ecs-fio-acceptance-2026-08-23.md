# 阿里云 ECS 首轮真实闭环验收

> 状态：真实受控实验完成；候选无统计显著收益，已回滚  
> 日期：2026-08-23  
> 适用边界：仅证明当前阿里云 ECS 上的代码、安全闭环和本次 fio 场景；不能外推腾讯云 CVM。

## 1. 验收结论

System Optimizer 已完成一次真实的 `snapshot → baseline → apply → verify → measure → rollback → final readback` 闭环。候选把根盘 NVMe scheduler 从 `mq-deadline` 临时切换为 `none`，测量完成后成功恢复为 `mq-deadline`。

候选没有产生可接受的性能收益：中位 IOPS 只增加约 0.0108%，95% bootstrap 改善区间为约 -0.0703% 至 +0.0801%，包含 0；中位 P99 完成延迟反而增加约 0.84%。引擎以 `no-improvement-policy` 停止，`recommended_candidate_id=null`，没有把测量噪声误报为优化成果。

这次结果证明的是“优化器能安全、诚实地拒绝无收益候选”，而不是“`none` 比 `mq-deadline` 更优”。

## 2. 实验合同

| 项目 | 显式值 |
|---|---:|
| workload | fio 4 KiB random read、direct I/O |
| 文件 | 1 GiB 专用文件 |
| 并发 | 2 jobs × iodepth 16 |
| 基线 / 候选 | `mq-deadline` / `none` |
| 重复 | 每个状态 5 次 |
| 单次 | 3秒 ramp + 15秒稳态 |
| 统计 | median、95%置信、2000次bootstrap、seed 20260823 |
| 接受条件 | 主指标改善的95%下界严格大于0 |
| 停止条件 | 单候选耗尽、无改善、安全失败、测量失败或600秒预算 |

## 3. 性能结果

| 指标 | `mq-deadline` 中位数 | `none` 中位数 | 点变化 | 判定 |
|---|---:|---:|---:|---|
| read IOPS | 4197.761 | 4198.215 | +0.0108% | 置信区间跨0，不接受 |
| read completion latency P99 | 31064.064 µs | 31326.208 µs | +0.8439%（变差） | 不接受 |

基线 IOPS CV 约 0.0401%，候选约 0.0353%；10份 fio 原始结果中20个 job 的 error 均为0。

## 4. 功能与安全验收

- 环境指纹缺陷已修复，CLI 在实机输出 `virtualization=kvm`。
- System Optimizer 专项：59 passed。
- 全仓：280 passed。
- safety state：`rolled_back`。
- 实验结束、工具 smoke 和最终回归后均读回 `none [mq-deadline]`。
- 最终无 fio、stress-ng、iperf3 或 pytest 进程残留。
- `stress-ng` 8 CPU worker、15秒运行成功，8个 worker 全部 passed。
- iperf3 loopback 运行成功；约61.9 Gbit/s只作工具链验证，不作云网络结论。

## 5. 服务器保留内容

没有销毁或清理服务器。保留了：

- `/opt/looper-system-opt-9b33190`
- `/opt/looper-system-opt-53faa7b`
- `/opt/looper-system-opt-ffa9107`
- `/opt/looper-system-opt-evidence/ecs-fio-20260823`
- `/tmp/looper-system-opt-*.tar.gz`

系统安装了 `stress-ng 0.17.06`、`fio 3.36`、`iperf3 3.16`；iperf3 服务保持 disabled/inactive。apt 安装新增21个包、约63 MB，并报告部分已有服务延后重启；本次未主动重启服务。

## 6. 证据入口

- `.artifacts/system-opt/aliyun-ecs-fio-20260823/acceptance-summary.json`：机器可读验收摘要。
- `.artifacts/system-opt/aliyun-ecs-fio-20260823/optimization-run.json`：优化器原始闭环结果。
- `.artifacts/system-opt/aliyun-ecs-fio-20260823/raw/`：10份 fio 原始 JSON。
- `.artifacts/system-opt/aliyun-ecs-fio-20260823/stress-ng-cpu.yaml`：CPU压力工具结果。
- `.artifacts/system-opt/aliyun-ecs-fio-20260823/iperf3-loopback.json`：loopback 工具验证结果。
- `.artifacts/system-opt/aliyun-ecs-fio-20260823/tool-inventory.json`：修复后的 KVM 环境与工具盘点。

## 7. 仍不能证明

- 不能证明腾讯云 CVM 上具有相同接口、权限、噪声和收益。
- 不能证明其他 fio 负载、文件大小、云盘规格或 scheduler 候选的效果。
- 不能用本次“无改善”推断所有系统配置都无优化空间。
- 没有保留任何候选配置，因此没有验证候选的跨重启持久化。
