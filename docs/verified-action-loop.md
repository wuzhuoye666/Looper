# 可验证动作闭环（本地纵向切片）

## 目的

现有 optimization mode 会生成候选、运行 Benchmark 并输出 Pareto 分析，但参数只存在于单次 run envelope 中，实验结束后没有一个持久化的“当前配置”，也不能证明无收益或失败时已经恢复原状态。

本切片增加一个保守的动作闭环：

```text
Snapshot
→ Baseline Readback
→ Baseline Benchmark
→ Apply Candidate
→ Candidate Readback
→ Candidate Benchmark
→ Paired Verification
→ Accept 或 Rollback
→ Rollback Readback
```

它首先服务于本地压缩 Benchmark，验证 Action Engine 与 Verification Engine 的最小合同。动作目标是 `.looper/verified-action/active-compression-config.json`，因此修改是真实、持久且可读回的，但只属于本地 demo，不是 OS、CVM 或数据库生产配置。

## 运行

```powershell
.venv\Scripts\looper.exe demo verified-loop
```

可调整的白名单参数：

```powershell
.venv\Scripts\looper.exe demo verified-loop `
  --compression-level 1 `
  --chunk-size 65536 `
  --repeats 3 `
  --minimum-improvement 0.05 `
  --maximum-ratio-regression 0.15
```

`chunk-size` 只允许 `4096`、`16384`、`65536`。配置文件写入采用同目录临时文件加原子替换，不执行自由文本 shell 动作。

## Web：Benchmark 完成后的优化按钮

完成一个 `optimization` 实验后，详情页请求：

```text
GET  /api/v1/experiments/{id}/post-optimization
POST /api/v1/experiments/{id}/post-optimization
```

GET 只分析，不修改状态。若 Benchmark 声明了尚未测试的低风险动作，页面显示“优化并重新测试”。POST 具有幂等语义：第一次调用创建并启动一个关联复测实验，再次调用返回同一个复测状态，不会重复创建。

当前动作声明位于 Benchmark manifest 的 `spec.x-extensions.postBenchmarkActions`：

```yaml
x-extensions:
  postBenchmarkActions:
    - id: larger-compression-chunks
      label: 增大压缩分块并复测
      risk: low
      applyMode: benchmark-parameter
      parameter: chunk_size
      value: 65536
      minimumImprovementRatio: 0.05
      guardMetric: compression_ratio
      maximumGuardRegressionRatio: 0.02
```

P0 只自动采用 `risk: low`、`applyMode: benchmark-parameter` 的动作；动作值还必须通过原 Benchmark 参数范围校验。每次复测只改变一个参数，其他参数固定为原实验最佳可行配置。

复测实验包含 baseline 和一个 candidate，至少各运行三次。决策规则为：

- 主指标提升置信下界达到 `minimumImprovementRatio`；
- 所有正确性和执行硬门槛通过；
- 若声明保护指标，其退化置信区间不超过 `maximumGuardRegressionRatio`。

全部满足才显示“建议保留”；确认无收益或保护指标越界则显示“保留原配置”；置信区间跨过门槛则显示“证据不足”，并建议增加重复。原实验保持不可变，复测实验 ID、动作和 before/after 值写入父实验 audit event。

这里的“保留”是配置决策：后续实验应使用候选参数。当前 Web P0 不会把参数直接部署到生产服务；远程 OS、数据库或 CVM actuator 必须先实现 snapshot/apply/readback/rollback 合同。

## 决策规则

每个 repeat index 的 baseline 与 candidate 使用相同随机种子。主指标为压缩吞吐，采用配对 bootstrap 的相对提升置信区间；次指标为压缩率，方向为越低越好。

- `accepted`：候选通过所有正确性门禁，压缩率退化未越界，且吞吐提升置信下界达到最小有效差异；候选配置保持 active。
- `rolled_back`：候选硬门禁失败、次指标越界、确认未达到最小收益，或执行/测量失败；恢复 baseline。
- `inconclusive`：置信区间跨过决策阈值；为安全起见同样恢复 baseline。
- `failed`：回滚自身失败，或回滚后读回 digest 与 baseline snapshot 不一致。

注意：`inconclusive` 不是失败的同义词，而是当前重复数不足以支持保留候选。

## 证据

每次运行创建独立 `verified-*` 目录，保留：

- baseline 与 candidate 每次真实 Benchmark 的 `result.json`、`metrics.jsonl` 和日志；
- 每组结果文件的 SHA-256；
- 修改前、请求候选和最终状态的 canonical digest；
- apply、readback、measurement、accept/rollback 的完整 audit trail；
- `verified-action.json` 最终决策文件。

回滚成功必须同时满足：动作执行完成，并且回滚后的 readback digest 等于修改前 snapshot digest。仅记录一条 rollback 日志不算成功。

## 当前边界

- 这是本地可信 demo，不是 IaaS 性能结论。
- 还没有通过 API/worker 对远程 target 执行动作。
- 还没有 Diagnosis Engine；候选由用户明确给出，不是系统根据瓶颈自由生成。
- BenchBase 和 DCPerf 仍保持 `stage0-adapter-only`。
- 不支持自由命令、sysctl、数据库参数、代码或 GPU kernel 修改。

## 下一步接入真实目标

后续 actuator 必须复用相同方法，而不是绕过合同：

1. `snapshot()`：保存目标当前状态与 digest。
2. `apply()`：只接受 Action Registry 的类型化参数。
3. `readback()`：从真实目标读取状态，禁止假定写入成功。
4. `rollback()`：恢复 snapshot。
5. rollback readback：证明最终状态确实恢复。

第一个远程动作建议选择进程 CPU affinity；第一个真实场景仍建议使用 BenchBase SmallBank，但必须先完成 PostgreSQL 快照恢复、独立客户端记账和可执行 runtime image。
