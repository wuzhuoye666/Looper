# ECS 8.134.104.213 系统初始调优实录（2026-08-23）

> 结论：静态初始调优全链（部署→状态→授权→校准→门→搜索闭环→回退验证）在
> 一台全新无负载机器上端到端执行成功；THP 三种模式在该机器对该 workload
> 均无显著差异（双向置信区间跨零），无推荐配置。会话还暴露并逐层通过了
> 三道目标机本地纪律：所有权授权、内核兼容下限、按基线状态校准的稳定门。
> 边界：阿里云 ECS 2 vCPU / 1.6 GiB / Ubuntu 22.04（5.15 内核）；不外推
> 大内存机器，更不外推腾讯云 CVM。

## 1. 环境

| 项 | 值 |
|---|---|
| 机器 | iZ7xvhz4676o4p0ohgytbuZ（8.134.104.213，密钥登录） |
| 规格 | 2 vCPU / 1608 MiB / Ubuntu 22.04.5 / 5.15.0-187-generic |
| 负载 | 无（无 benchmark worker，load ≈ 0） |
| 代码 | 3b01722（当日全部合并后），部署 sha256 060b7d8f 字节验证一致 |
| Python | 3.11.0rc1 + 专用 venv（22.04 系统 3.10 不满足 StrEnum 要求） |

## 2. 三道目标机本地纪律（逐层 fail-closed 后逐层补正）

1. **所有权**：22.04 的 virtual-guest tuned profile 不写 THP，活值 madvise
   无来源解释 → 候选被安全层拒绝（"ownership is unknown"）。补正：
   `authorize-state` 操作员显式授权（declaration digest 2b48e1f6）。
   首两轮 run 的候选因此空转——如实记录，其 no-improvement 不是测量结论。
2. **内核兼容**：M2 manifest 烤有 kernel_min 6.8（24.04 机器），本机 5.15
   → preflight 拒绝。补正：本机版 manifest（kernel_min 5.15）+ 重新采集
   状态证据 + 重新授权（authorized-state-evidence-v2）。
3. **按基线状态校准的稳定门**：never 基线批次 CV 8.9% 超 madvise 态门
   4.64% → 拒绝。这本身是**诊断证据**：THP=never 使这台小机器内存行为噪
   声约 2-3 倍。补正：在 never 态重新校准（CV 9.90% → 门 0.1257），
   门绑定基线状态，不跨状态复用。

## 3. 闭环结果（run2f，never 基线，never 态门）

| 配置 | 带宽中位数 MiB/s | 估计 | LCB95 | UCB95 | 判定 |
|---|---:|---:|---:|---:|---|
| `never` 基线 | 23,382 | — | — | — | never 态门通过 |
| `always` | — | -0.09% | -0.54% | +2.64% | 未接受，已回滚 |
| `madvise` | — | -1.46% | -2.29% | +0.92% | 未接受，已回滚 |

停止原因 `no-improvement-policy`；候选轮 2、attempts 3；两候选均真实
施加→测量→回滚（safety=rolled_back，读回验证在批次证据中）。madvise 基
线方向（run1 系列）同样无候选胜出。**两方向置信区间均跨零**。

## 4. 与 8vCPU 机器的对照（诚实范围声明）

服务器 1（8 vCPU / 15 GiB / M2 数据）上 never 相对 madvise 为 -3.8%
（LCB -5.6%）——效应存在。本机（2 vCPU / 1.6 GiB）上同 workload 同协议
（threads 适配为 2）效应消失。结论：**THP 效应是机型相关的**，1 MiB 顺序
写在小内存机器上不受大页影响。发现成功演示（从劣化基线找到改善并守住）
需要 8vCPU 级机器执行——本机在两个基线方向都只能给出有界的负结论。

## 5. 证据与计数

- 命令台账 N0001-N0032（logs/，逐条 sha256）；远端会话目录整取 173 文件
  （remote/），含两组校准批次、两个稳定门、两轮授权状态证据、全部测量
  原始文本与干扰检查。计数口径：按文件计，不去重。
- 会话协议与资产：examples/system-optimizer/session-assets/8134-*（本机
  manifest、两版硬门禁协议、校准协议、never 基线）。
- 机器收尾：THP 读回 `always [madvise] never`（恢复默认）；租约全部释放。
