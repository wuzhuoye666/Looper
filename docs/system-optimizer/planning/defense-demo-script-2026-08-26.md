# System Optimizer 答辩演示脚本（2026-08-26）

> 定位：一页故事线 + 现场演示步骤 + 高危问题口径。所有数字都有落盘证据与 sha256，
> 没有证据的数字一律不出现。

## 1. 电梯陈述（30 秒版）

我们给 Looper 造了一个**证据绑定的 Linux 系统调优引擎**：九层架构（执行器→安全→
测量→采集→组件优化器→回退→负缓存→引擎），不内置任何"最优配置"的假设，而是把
"改配置是否值得"变成一个可回放的统计实验。今天（8/25）在通过平台购买的一台全新
阿里云 8vCPU 机器上，引擎在默认配置上**自主发现 +15.59% 的真实提升**并给出晋升
推荐，随后把竞争假设（另一个方向）**按退化门禁拒绝**，全程零人工介入、结束自动
还原、证据链可离线复算。

## 2. 核心数字卡

| 数字 | 值 | 证据 |
|---|---|---|
| 发现效应 | THP madvise→always **+15.59%**（44684→51649 bogo-ops/s） | 相位 1 复测 5 窗，CV 0.65% |
| 拒绝效应 | madvise→never −1.31%，多窗破 2% 退化界 → safety-triggered | 相位 2，stop=true |
| 统计门槛 | MDE 2%（用户显式批准），bootstrap LCB 裁决 | business-policy.json |
| 安全性 | 双相位结束 THP 恢复 madvise；零 guard/lease/attention；receipt 全 terminal | 回放验证 ALL-PASS |
| 成本 | 双臂全程机器时间 <1.5h ≈ 1 元 | 按量计费 |

## 3. 现场演示（二选一）

### 方案 A（推荐，~3 分钟）：证据包回放

```
PYTHONPATH="services/api;packages/core;packages/benchmark-sdk" python \
  .artifacts/discovery-8vcpu-20260825/verify_discovery.py \
  .artifacts/discovery-8vcpu-20260825/downloaded-evidence
```

讲解点（对着输出讲）：run digest 重算通过 = 证据没被篡改；receipt_terminal=true =
每次配置写入都有收据且闭环；residuals=[] = 机器上零残留；两个 run 的 stop_class
（target-met vs safety-triggered）= 同一台机器上"该赢的赢、该拒的拒"。

### 方案 B（备用，~21 分钟，有风险）：真机重跑一相

机器 `47.104.25.156` 若仍在：`/opt/looper-discovery/run_discovery_phase.py` 可整相
重跑。风险：按量计费、时间超预算、现场网络。**除非评委明确要求，否则用方案 A。**

### 辅助（~1 分钟）：synthetic 全链

`system-opt m3-demo --workspace <空目录>` → 5 窗 target-met + promotion + 在线路由 +
负缓存 + Profile 报告，证明功能链完整（8/25 已在主线 8439654 独立复跑）。

## 4. 时间线素材（三轮真机，层层递进）

1. **8/23**：阿里云 ECS multiround（调度器/nomerges 候选全拒，效应≈0）——门禁在
   "无效应"时正确说不。
2. **8/25 晨（REAL-M3-01）**：低配机真实闭环功能校准，THP 双候选被 26.7% 粗 MDE
   拒绝——**不是无效应，是 CV 10.68% 测不出**。
3. **8/25 午后（REAL-DISC-01）**：换测量力足够的机器（CV 0.65%），同引擎同流程，
   +15.59% 晋升 + 反方向安全拒绝——**引擎没变，变的是实验设计**，这就是 Benchmark
   方法论（导师清单课题包 B 的核心主张）。

## 5. 高危问题口径（Q&A 弹药）

| 可能被问 | 口径 |
|---|---|
| 为什么默认 madvise 不是最优？ | THP 三档语义：always 全量大页（吞吐型受益、尾延迟受害）；madvise 只给显式声明区域（stress-ng 这类不自声明的负载等于没用上）；never 全关。**没有普适最优值，所以要测** |
| 那为什么不全局改 always？ | 数据库/NFV 类负载 always 会因内存压缩造成尾延迟尖刺；本结论只对声明的 workload/argv/环境成立（合同 limitations 已写） |
| 8/25 早为什么拒绝？ | 测量力：CV 10.68% 强制 MDE 26.7%，3-5% 的真实效应不可能被接受。正确行为是不说谎 |
| 只优化一个指标？ | 会话级设计选择。引擎支持多指标 O0/SLO 列表/独立退化门（可"优化 A 盯着 B"）；8/23 fio 多轮就是双指标 Pareto。导师完整三层目标函数在 M6+/S4-V2 范围 |
| 已知缺口？ | DYN-END-01（max_windows 耗尽 stop=false）——真机发现、已登记、设计已定稿（A 案 gate v1alpha3），实现进行中；证据中已显式标注 |
| 为什么还没 REAL-S9？ | 硬前置"真实 accepted candidate"今天才首次满足；需第二环境重新校准，不能复制阈值。路径已冻结在交接计划 |
| 怎么保证不把机器搞坏？ | L1 安全层：manifest/所有权声明/租约/fencing/收据；写前授权白名单；相位结束无条件恢复；attention/恢复 CLI。今天双相位零残留即证据 |
| 和 Looper 平台的关系？ | 机器就是**通过 Looper 平台购买**的（云市场→凭据→下单→SSH 采用）；系统优化器已合入 main（595eeb2），M4 平台化路径已冻结 |

## 6. 证据包清单（sha256）

- 相位 1（always，晋升）：`02b77f20893f2ac56b985bdee4bb272d29ab33db0a25c908c5c5b4477bb83dbe`
- 相位 2（never，安全拒绝）：`4598915084fdc300d7d5d2fde5011289a21f32881f7ee822103bdbb34f85d3a4`
- 回放验证输出：`.artifacts/discovery-8vcpu-20260825/downloaded-verification.json`
- 失败尝试审计（TTL 合同拦截，fail-closed 证据）：`run-always-20260825-failed-ttl/`
- 8/25 晨低配机闭环（对照材料）：`.artifacts/real-demo-2026-08-25/`

## 7. 演示纪律

- 不出现的说法："全自动调优不需要人"（授权边界是人拍板）、"always 更好"（负载依赖）、
  "优化器保证收益"（PERF-CAND-01 不保证成功，拒绝也是有效结果）。
- 所有数字出口前对照本文表格；被问倒的数字回答"我确认证据后回答"，不现场编。
