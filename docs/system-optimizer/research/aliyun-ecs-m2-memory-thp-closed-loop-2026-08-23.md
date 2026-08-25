# 阿里云 ECS M2 内存 THP 候选闭环实录（2026-08-23）

> 结论：`madvise` 基线通过目标机稳定性硬门禁；`always` 与 `never` 均安全施加、
> 测量并回滚，但没有任何候选满足主指标 LCB95 > 0，因此没有推荐配置。
> 边界：阿里云 ECS Ubuntu 24.04 / Linux 6.8 KVM guest、固定 sysbench 内存写协议；
> 不外推腾讯云 CVM，也不外推其他内存 workload。

## 1. 阈值口径

稳定门禁来自首次冻结校准批次，不使用跨任务默认值：

1. 每次从同一组 7 个样本有放回抽 7 个值；
2. 同一个重采样同时计算样本标准差和均值；
3. 计算 `CV_b = sample_stdev_b / abs(mean_b)`；
4. 2,000 次重采样、固定种子 `20260823`；
5. 单侧 95% 经验分位数为 `0.02922585447690954`。

formula id 为 `F-PROJECT-PRESSURE-CV-BOOTSTRAP-UPPER/v1alpha1`。它是这台 ECS、
这套输入和这段时间的初始 gate；协议、环境或目标机改变都必须重校准。

## 2. 并发异常与处置

第一次正式基线的 7 个带宽值中位数为 83,496.45 MiB/s、CV 为 4.762%，超过硬门禁，
优化器在任何候选 apply 前停止。随后只读检查发现 Looper worker 同时运行 Phoronix
PHPBench，子进程约占一个 CPU 满核；该批次被定性为共享目标争用证据，未进入阈值重校准。

新增进程干扰门禁后，每个测量批次在 warmup 前和 measure 后检查显式进程模式。保存的
证据只含进程名、PID、命中模式和 command digest，不保存原始 command line。成功闭环中
preflight/postflight 命中数均为 0。门禁仍有 TOCTOU 边界，后续应升级为调度层独占租约或
cgroup/worker 协调，而不是无限扩展进程名列表。

## 3. 成功闭环结果

统一协议：每个批次执行 `干扰检查 → prepare → 5s warmup → 7×5s measure → 干扰复查
→ verify → cleanup`。基线和两个候选共享 3 次 attempt，无周期基线刷新。

| 配置 | 带宽中位数 MiB/s | 样本 CV | 相对基线估计 | LCB95 | 判定 |
|---|---:|---:|---:|---:|---|
| `madvise` 基线 | 100,283.98 | 2.281% | — | — | 稳定门禁通过 |
| `always` | 98,270.29 | 1.804% | -2.222% | -3.811% | 未接受，已回滚 |
| `never` | 96,871.30 | 1.644% | -3.766% | -5.557% | 未接受，已回滚 |

停止原因为 `no-improvement-policy`，候选轮数 2、测量 attempts 3、推荐候选为空。
最终 `/sys/kernel/mm/transparent_hugepage/enabled` 读回 `always [madvise] never`，即
活动值仍为 `madvise`；Looper worker 仍运行。

## 4. Raw 到候选的绑定

四个时间戳各有 7 份 measurement 文本：一组是被争用门禁拒绝的基线，后三组依次为
成功基线、`always`、`never`。对原始文本重新解析带宽数组后生成 `MetricEvidence`
摘要（`primary_metric_evidence_digest`），与 optimization run 中各候选
`improvements` 的 per-metric `candidate_digest` 完全相同，因此不是仅凭文件名
推测配置归属。机器可读摘要中每个候选同时携带该单指标摘要与整批
`measurement_batch_digest`（= run 的 `measurement_digest`）；两级摘要的命名与
绑定规则见[指标契约](../contracts/metric-contract.md)的证据摘要命名一节
（该命名规范由本次发现的 A 级字段歧义确立）。

## 5. 证据计数

计数口径：按本地实际文件路径计，不按内容、扩展名或非空状态去重。

| 分组 | 文件数 | 字节数 |
|---|---:|---:|
| 远端 raw 副本 | 62 | 31,036 |
| 远端控制证据 | 4 | 34,879 |
| 首次失败基线 batch 独立副本 | 1 | 1,030 |
| 合计 | 67 | 66,945 |

raw 中有 29 个 `.txt`、29 个零字节 stderr `.log` 和 4 个 JSON。敏感模式扫描未命中
密码、token、私钥或 `tcp_fastopen_key`。

## 6. 能证明与不能证明

能证明：

- 稳定性门禁能在共享环境争用时阻止候选 apply；
- 独占窗口恢复后，THP 三值域能通过真实安全链完成两候选闭环；
- 两个候选都未达到任务接受条件，系统正确保留原 `madvise`。

不能证明：

- `madvise` 是跨 workload、跨时间或跨机器的最优 THP 策略；
- 当前 sysbench event p95 等于 MESS loaded memory latency；
- 进程模式门禁能识别所有外部干扰；
- 阿里云结果可迁移到腾讯云 CVM。

机器可读摘要与 raw 位于
`.artifacts/system-opt/m2-memory-thp-search-20260823/`。
