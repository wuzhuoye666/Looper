# 阿里云 ECS M2 CPU governor 候选闭环实录（2026-08-23）

> 结论：tuned 所有权按合同完成人工交接（stop 后 governor 回退为内核默认 `schedutil`，
> 该状态即真实基线）；在新基线下重新校准 CV 门禁后，完成 `powersave` 与
> `performance` 两候选真实闭环。`powersave` 批次 CV 0.00441 超过硬门禁 0.00311 被
> 拒绝；`performance` 估计 +0.17% 但 LCB95 为 -0.20%，未满足采纳条件。两个候选均
> 已回滚，最终 governor 读回 `schedutil`，无推荐配置。
> 边界：阿里云 ECS Ubuntu 24.04 / Linux 6.8 KVM guest、固定 stress-ng matrixprod
> 协议；不外推腾讯云 CVM，也不外推其他 CPU workload。

## 1. 所有权交接

- 交接前：`tuned` active（profile `virtual-guest`），8 个 per-vCPU policy 的 governor
  均为 `performance`（tuned 设置）。
- 操作者执行 `systemctl stop tuned` 后，governor **回退为 `schedutil`**（内核默认）。
  这一事实说明"停掉 tuned"本身就是一次基线变化：此前在校准（tuned/performance 状态）
  下派生的 CV 门限不再适用，本闭环全部在新基线（schedutil）下重新校准。
- 交接后以 tuned profile 文件为 source scope 采集状态证据；governor 无外部声明
  （ownership unknown），随后由操作者逐项授权给 actor `zcode-m2-cpu-20260823`。

## 2. 建模与阈值口径

- governor 建模为**一个逻辑配置项** `cpufreq-governor-uniform`：helper
  （`examples/system-optimizer/cpu_governor_control.py`）把值写入全部 8 个
  `policy*/scaling_governor`，读回要求全部一致，任何分歧即失败（fail-closed）。
  因此单次变更计数为 1，不存在逐 policy 的组合爆炸。
- 授权域收敛为 `[schedutil, performance, powersave]`（能力域为内核报告的全部 6 个）。
- CV 门禁从 schedutil 基线 7 样本冻结批次派生：bootstrap 2000 次、种子 20260823、
  单侧 95% 上界 **0.0031146792925256066**（观测 CV 0.002384），
  formula `F-PROJECT-PRESSURE-CV-BOOTSTRAP-UPPER/v1alpha1`，
  校准证据 digest `sha256:c2f57638f436a677176589e694b9ae7365dabb35c34bb021daf0d021e608790c`。
- 主指标 scale = 冻结校准中位数 9453.494471 bogo-ops/s。

## 3. 闭环结果

统一协议：干扰检查 → prepare → 5s warmup → 7×5s measure → 干扰复查 → verify →
cleanup；前后干扰窗口命中均为 0。

| 配置 | bogo-ops/s（均值） | 批次 CV | 相对基线估计 | LCB95 | 判定 |
|---|---:|---:|---:|---:|---|
| `schedutil` 基线 | 9,453.6 | 0.00076 | — | — | 稳定门禁通过 |
| `powersave` | 9,456.4（raw） | **0.00441** | 不可评 | 不可评 | 批次被稳定性硬门禁拒绝，已回滚 |
| `performance` | ≈9,470 | 合格 | +0.17% | **-0.20%** | 未接受，已回滚 |

停止原因为 `no-improvement-policy`；尝试数 3（基线 1 + 候选 2）。最终
`policy0..7/scaling_governor` 读回全部 `schedutil`。

两个值得记录的观察：

1. powersave 在该 guest 上并未降低吞吐均值（≈9456，与基线几乎相同），但批次波动
   显著增大（出现 9379/9523 两个离群样本）。门禁拒绝的是"不可靠的批次"，不是
   "慢的配置"——这正是分布证据合同想要的行为。
2. performance 相对 schedutil 无显著收益（LCB 含零），说明该 KVM guest 上
   schedutil 对持续满载的调频已经足够；"换成 performance 会更快"在本协议下
   不成立，不能作为默认建议。

## 4. Raw 到候选的绑定

三个时间戳批次各有 7 份 stress-ng YAML（基线、powersave、performance）。powersave
批次原始值可直接从 YAML 复算 CV=0.00441，与门禁拒绝一致；performance 批次以
`MeasurementBatch.digest` 与 `improvements` 的 per-metric 摘要双重绑定进
optimization run（命名遵循[指标契约](../contracts/metric-contract.md)的证据摘要
命名一节）。

## 5. 能证明与不能证明

能证明：

- tuned 所有权交接流程（停 tuned → 状态证据 → 逐项授权）在真实目标上可用；
- 基线状态变化后按合同重校准门禁、再进入候选闭环的完整链路可执行；
- 稳定性硬门禁能拒绝高波动批次（powersave），采纳规则能拒绝 LCB≤0 的候选
  （performance）；
- governor 作为单一逻辑项跨 8 个 policy 的一致性施加与回滚可靠。

不能证明：

- `schedutil` 与 `performance` 在该 guest 上等价（只能说本协议未检出显著差异）；
- powersave 的波动来源（governor 调频行为或环境噪声，未做归因下钻）；
- 结论对其他 CPU workload（如突发型、低占用型）成立；
- 阿里云结果可迁移到腾讯云 CVM。

机器可读摘要与 raw 位于 `.artifacts/system-opt/m2-cpu-governor-search-20260823/`
（闭环）与 `.artifacts/system-opt/m2-cpu-governor-calibration-20260823/`（校准）。
