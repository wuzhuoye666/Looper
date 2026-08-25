# 动态相位真机会话模板（8vCPU discovery 演示，2026-08-25 调优日用）

> 目标：在 8vCPU 机器上跑第一个**真机动态相位**：外部 stress-ng 持续负载 →
> 症状（SLO 未达）→ 双竞争假设（THP madvise / governor performance）→ L1 安全
> 干预 → 业务复测 S6/S7 → S9 晋升。与静态链路（M2R 已全绿）互补：静态证
> 「比较与门禁」，动态证「发现与闭环」。

## 模板内容与缺口

| 文件 | 状态 | 明天要补什么 |
|---|---|---|
| `workload-contract.yaml` | ✅ argv digest 已对定稿 argv 计算 | 若 runner argv 改动→重算 digest；依基线填 SLO bound |
| `hypothesis-proposals.yaml` | ✅ 双竞争假设（rank 显式） | 无（change 键须与本机 manifest 对齐核对） |
| `gate-contract.json` | ⚠️ 数值占位 | 依基线填 SLO bound；预算按会话时长定 |
| `promotion-contract.json` | ✅（2 观测 / 2 时间块 / 1 环境） | 无 |
| `business-policy.json` | ⚠️ 数值占位 | scale/MDE/minimum_samples 依基线校准填 |
| `baseline-batch.json` | ❌ 不预建 | 步骤 3 在机上实测生成（禁编造） |
| `o1-collection-plans.json` | 可选 | 若开活体 O1：本机环境 digest 绑定后生成 |

## 上午机上流程（runbook）

1. **环境与清单**（复用 M2R 静态链路纪律）：state-inventory → authorize-state
   （所有权未知项操作员授权）→ 确认内核兼容下限（参照 8134-memory-thp-manifest
   的 kernel_min 机制，8vCPU 机内核 ≥5.15 即可）。
2. **基线校准**：外部起 stress-ng（与合同 argv 完全一致）跑 ≥5 轮
   `--yaml` 输出落盘；取 bogo-ops/s 序列：
   - 填 `baseline-batch.json`（identity 用
     `build_business_batch_identity(contract, "steady")` 生成，勿手写）；
   - 依序列均值/极差填 SLO bound（约基线 97%）与 business-policy 的
     scale / minimum_effect / minimum_samples。
   - ⚠️ 若基线态 CV > 10%：先做静态 CV 门校准（M2R 同款流程）再回来——
     动态相位复用同一稳定纪律。
3. **THP 基线态**：演示剧本从 `never` 基线出发（该态 M2 实测有真实损失空间）；
   开跑前 `cat /sys/kernel/mm/transparent_hugepage/enabled` 确认，写入会话台账。
4. **外部负载会话脚本**（DeepSeek 泳道交付）：按
   `docs/system-optimizer/contracts/dynamic-session-files.md` 起压落盘
   `windows/`；引擎侧 `dynamic-run` 读窗推进。
5. **运行动态相位**：
   ```
   looper system-opt dynamic-run --session <dir> --manifest <m2r-manifest> \
     --state-evidence <evidence> --backend local-linux \
     --target-id <id> --owner-id <owner> --lease-root <dir> --lease-ttl-seconds 14400 \
     --allow-executable <读/写命令白名单 同静态 run> --writable-root <同静态> \
     --max-windows 12 --probe-top-k 2 --verification-windows 2 \
     --o1-plans o1-collection-plans.json --o1-window-seconds 30 \
     --o2-source live --o2-window-seconds 10 \
     --enable-real --confirmation I_UNDERSTAND_LINUX_CONFIG_WRITES \
     --output dynamic-run.json
   ```
6. **判读**：`promotion.promoted=true` 且 `candidate_id=hyp-thp-madvise`
   即动态发现成立（与 M2 静态测量交叉验证）；`control/phase-restoration.json`
   必须为 `kept`——机器回到 never 基线态（晋升≠持久变更）。
7. **台账**：全程 N/C 编号 ndjson 记账（记账先行），与 M2R 会话同款。

## 边界提醒

- 引擎永不启动负载（SO-D020）；负载身份不符→身份漂移立即停相位。
- 拒绝的假设自动恢复 pre-apply 值；恢复失败=安全事件停相位。
- 相位收尾无条件恢复到相位起点；持留变更需操作员显式决定（本演示不做）。
