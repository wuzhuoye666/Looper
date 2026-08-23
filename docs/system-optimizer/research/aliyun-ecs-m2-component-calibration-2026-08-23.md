# 阿里云 ECS M2 组件压力首次校准（2026-08-23）

> 结论：CPU、Memory 和 Network-loopback 已稳定出数；NUMA 因单节点明确不可测。
> 边界：Ubuntu 24.04 / Linux 6.8 KVM guest 事实，不外推腾讯云 CVM。
> 定性：首次 `report-only` 校准，不是配置候选收益实验。

## 1. 环境与工具

- 8 vCPU，1 socket，4 core × 2 thread，单 NUMA node 0。
- stress-ng 0.17.06、sysbench 1.0.20、iperf3 3.16、numactl、perf、fio、ethtool
  均可获取。
- 当前 ECS 的 `perf` 通用硬件事件可读；这不证明腾讯云 CVM 会透传 PMU。
- 共享 Looper worker 在实验前已由外部流程从旧 PID 换为 71889；新 PID 在全部压力结束后
  仍存活。时间证据显示 PID 更换早于本轮压力，不归因于探针。

## 2. 原始证据计数

计数口径：按本地证据目录下实际文件路径计，不按内容、扩展名或非空状态去重。

| 目录 | 文件数 | 字节数 |
|---|---:|---:|
| CPU | 18 | 9,089 |
| Memory | 18 | 9,305 |
| Network loopback | 34 | 68,709 |
| NUMA capability | 1 | 567 |
| 合计 | 71 | 87,670 |

其中 32 个 stderr 文件为 0 字节：CPU 8、Memory 8、Network 16。它们未被删除，表示对应
成功运行没有 stderr。敏感字符串扫描未命中密码、token、私钥或 `tcp_fastopen_key`。

证据根目录：`.artifacts/system-opt/m2-component-calibration-20260823/`。

## 3. 实测结果

稳定性统一按样本标准差计算：`CV = sample_stdev / abs(mean)`。这是报告值，不是已批准门禁。

| 组件/指标 | 重复协议 | 中位数 | 范围 | 样本 CV | 结论 |
|---|---|---:|---:|---:|---|
| CPU bogo ops/s | 1 次 5s warmup；7 × 5s；8 worker；matrixprod | 9,430.635 | 9,426.091–9,440.945 | 0.0532% | 稳定出数 |
| Memory bandwidth MiB/s | 1 次 5s warmup；7 × 5s；8 thread；1MiB sequential write | 90,624.94 | 89,956.90–95,580.55 | 2.5382% | 稳定出带宽 |
| Memory event p95 ms | 同上 | 0.13 | 0.11–0.13 | 7.7422% | 0.01ms 分辨率偏粗，不作为精确 gate |
| Network receive Gbps | 1 次 warmup；7 × (1s omit + 3s)；2 stream；loopback | 110.8967 | 108.8705–120.9465 | 3.6689% | 仅协议栈稳定出数 |
| Network retransmits | 同上 | 0 | 0–0 | 不适用 | 基线为零，禁止计算相对 CV |
| NUMA | numactl topology | 不适用 | nodes=[0] | 不适用 | `available=false`，未施压 |

## 4. 异常与修复记录

首次 CPU 探针错误要求 stderr 同时包含固定成功文本，但该 stress-ng 组合把日志送到 journald，
stderr 为 0 字节。退出码和 YAML 指标实际有效；失败批次未进入结果。修复后成功条件改为：

1. 进程退出码为 0；
2. YAML 根和 CPU metric 存在；
3. throughput 为正且有限；
4. 原始 YAML 保留。

这是一项字段语义实测修复，不能再凭“通常会输出到 stderr”写 gate。

## 5. 能证明与不能证明

能证明：

- 四类探针能在目标 Linux guest 按显式阶段安全执行和清理；
- CPU/Memory/Network-loopback 在本次短协议下能重复出数；
- 单 NUMA 环境会生成结构化 unavailable 证据，而不是伪造远端节点结果。

不能证明：

- 任何配置候选带来性能收益；
- 当前校准值已经满足正式稳定性门禁；
- loopback 结果代表 virtio NIC、VPC 或跨机网络；
- sysbench p95 等于 loaded memory latency；
- 阿里云结果可迁移到腾讯云 CVM。

## 6. 下一步门禁

1. 由实验合同批准各组件稳定阈值或批准阈值生成规则；
2. CPU/Memory 选择有官方来源、目标可访问、所有权明确且与探针语义相关的候选；
3. Network 取得第二受控 peer 后再建立 NIC/VPC 证据；
4. NUMA 等待至少双节点目标，不在本 ECS 上继续搜索；
5. 各组件局部候选完成后做混合压力组合复验。

## 7. 代码验证口径

- System Optimizer：15 个测试文件，90 个测试函数，pytest 收集 93 个 case，93/93 通过。
- 仓库：pytest 收集 314 个 case；排除既有 cloud confirmation token flaky 后
  313/313 通过。
- 既有 flaky 独立 basetemp 顺序复跑 10 次为 9 pass / 1 fail；本轮没有修改 cloud
  模块，不能记为全量 314/314。
- Ruff 对 `packages`、`services`、`tests` 和 System Optimizer examples 全部通过。
