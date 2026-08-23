# Linux 官方配置候选目录（M1，20 项）

> 状态：M1 documented candidate catalog；尚未通过 CVM 目标验证  
> 计数口径：`sysctl、cpufreq/governor、THP、I/O、NUMA、network` 各 3 项，
> 再加 IRQ affinity 与 MTU，各逻辑项只计一次，总计 `6 × 3 + 1 + 1 = 20`。  
> 证据边界：参数语义来自 kernel.org；当前状态来自本机 WSL2 只读探测。WSL2
> 只验证代码的 available/unavailable 分支，不能证明 CVM 存在性、权限、默认值、
> 动态域、收益或安全性。

## 官方来源

- sysctl：[VM sysctl](https://docs.kernel.org/admin-guide/sysctl/vm.html)
- cpufreq/governor：[CPU Performance Scaling](https://docs.kernel.org/admin-guide/pm/cpufreq.html)
- THP：[Transparent Hugepage Support](https://docs.kernel.org/admin-guide/mm/transhuge.html)
- I/O：[Block queue sysfs](https://docs.kernel.org/block/queue-sysfs.html)
- NUMA：[kernel sysctl（NUMA balancing）](https://docs.kernel.org/5.17/admin-guide/sysctl/kernel.html)
- network：[IP sysctl](https://docs.kernel.org/networking/ip-sysctl.html)
- IRQ：[SMP IRQ affinity](https://docs.kernel.org/core-api/irq/irq-affinity.html)
- MTU：[Linux sysfs ABI：`/sys/class/net/<iface>/mtu`](https://docs.kernel.org/admin-guide/abi-testing-files.html)

NUMA scan 参数的官方说明来自 kernel.org 的 5.17 版本化文档；它们在本机 WSL2
6.18 定制内核中不存在。因此这里只能登记为 CVM 候选，必须根据 CVM 实际内核版本
重新核对，不能以旧版官方文档推断新内核一定保留接口。

## 20 项核对结果

| # | 类别 | 逻辑路径 | 官方语义摘要 | WSL2 只读状态 | 当前处置 |
|---:|---|---|---|---|---|
| 1 | sysctl | `/proc/sys/vm/swappiness` | 交换页与文件页回收的相对 I/O 成本权衡，最优值依赖 workload | `60` | observation-only |
| 2 | sysctl | `/proc/sys/vm/dirty_ratio` | 写入进程开始参与回写的可用内存百分比阈值；与 `dirty_bytes` 互斥 | `20` | observation-only |
| 3 | sysctl | `/proc/sys/vm/dirty_background_ratio` | 后台 flusher 开始回写的百分比阈值；与 bytes 形式互斥 | `10` | observation-only |
| 4 | cpufreq/governor | `/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor` | 当前 policy 的 governor；合法值来自 `scaling_available_governors` | 路径不存在 | unavailable，不入搜索 |
| 5 | cpufreq/governor | `/sys/devices/system/cpu/cpu0/cpufreq/scaling_min_freq` | policy 允许的最低频率，单位 kHz，不能高于 max | 路径不存在 | unavailable，不入搜索 |
| 6 | cpufreq/governor | `/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq` | policy 允许的最高频率，单位 kHz，不能低于 min | 路径不存在 | unavailable，不入搜索 |
| 7 | THP | `/sys/kernel/mm/transparent_hugepage/enabled` | 全局 THP 策略；`never` 也不等于所有 collapse 情形绝对禁用 | `always [madvise] never` | observation-only |
| 8 | THP | `/sys/kernel/mm/transparent_hugepage/defrag` | 控制 THP 分配失败后的直接或后台 reclaim/compaction | `always defer defer+madvise [madvise] never` | observation-only |
| 9 | THP | `/sys/kernel/mm/transparent_hugepage/shmem_enabled` | 控制 tmpfs/shmem 的 THP 策略；可选值与内核能力相关 | `always within_size advise [never] deny force` | observation-only |
| 10 | I/O | `/sys/block/sda/queue/scheduler` | 当前及可用 I/O scheduler；设备/驱动特定 | `[none] mq-deadline kyber` | observation-only；CVM 重枚举设备 |
| 11 | I/O | `/sys/block/sda/queue/nr_requests` | 块层读/写请求分配数量，可能按 block cgroup 分池 | `633` | observation-only；CVM 重枚举设备 |
| 12 | I/O | `/sys/block/sda/queue/read_ahead_kb` | 设备文件系统预读上限，单位 KiB | `8192` | observation-only；CVM 重枚举设备 |
| 13 | NUMA | `/proc/sys/kernel/numa_balancing` | 启用/禁用自动 NUMA balancing | 路径不存在 | unavailable，不入搜索 |
| 14 | NUMA | `/proc/sys/kernel/numa_balancing_scan_period_min_ms` | 每任务最短扫描周期，控制最高扫描率 | 路径不存在 | unavailable；CVM 按内核复核 |
| 15 | NUMA | `/proc/sys/kernel/numa_balancing_scan_period_max_ms` | 每任务最长扫描周期，控制最低扫描率 | 路径不存在 | unavailable；CVM 按内核复核 |
| 16 | network | `/proc/sys/net/core/somaxconn` | socket listen backlog 上限 | `4096` | observation-only |
| 17 | network | `/proc/sys/net/ipv4/tcp_max_syn_backlog` | 单监听器未完成 ACK 的 SYN_RECV 请求上限 | `512` | observation-only |
| 18 | network | `/proc/sys/net/ipv4/tcp_congestion_control` | 新连接默认拥塞控制算法；合法值来自 available 列表 | `cubic` | observation-only |
| 19 | IRQ | `/proc/irq/<IRQ>/smp_affinity_list` | 某 IRQ 允许的 CPU 列表；managed IRQ 可能不允许用户修改 | IRQ 0 为 `0-7` | 仅证明接口分支可读；CVM 按设备/IRQ 映射 |
| 20 | MTU | `/sys/class/net/<iface>/mtu` | 网络接口 MTU 的 sysfs ABI；接口和 network namespace 特定 | `eth0=1500` | observation-only；CVM 按 NIC/namespace 重枚举 |

## 工具、权限与缺接口的执行规则

1. 接口不存在不是值 `0`：记录 `unavailable`，从动态搜索域移除，其他组件可继续。
2. 存在但不可读：记录 `permission-denied`；不能用默认值或论文值补齐。
3. 可读但不可写：保留 observation；人工修改和自动候选都不得 apply。
4. 工具缺失：先检查是否存在无损替代路径。例如 WSL 没有 `sysctl` 命令，但
   `/proc/sys` 可直接只读，因此采集可以继续；这不代表依赖 `sysctl` 的写路径可用。
5. 关键 workload、正确性检查、snapshot、verify 或 rollback 工具缺失：preflight
   fail-closed，本轮不得 claim；可选微指标工具缺失只降低覆盖率并必须报告。
6. 安装软件是独立的外部变更：CVM 先生成依赖缺口和安装计划，记录发行版、仓库、
   包版本、磁盘/网络条件与回滚办法，得到用户授权后才安装；优化器不静默安装。

## M1 验收含义

本表完成的是“20 个官方语义候选 + WSL2 available/unavailable 分支验证”，不是
“20 个 CVM 可调项”。最终 M1 在 CVM 上仍需重新采集环境指纹，并逐项取得存在性、
可读性、可写性、生效读回、持久化/所有权、动态合法域、依赖/互斥、snapshot 和
rollback 证据。只有通过这些门的子集才能进入有限闭环搜索。
