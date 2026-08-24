# System Optimizer 多 Agent 任务与合入台账（2026-08-24）

> 状态：current；本文件是 2026-08-24 起唯一任务/交付/验收/合入登记本。
> 规则来源：`governance/collaboration-protocol.md` 与仓库 `AGENTS.md` §十四。
> 主线：`system-optimizer-impl`；统一集成与 push 责任人：`glm5.3`。

## 记录字段与完成定义

每项任务必须登记：task id、owner、worktree/branch、实际基线、依赖、写集合、禁止写
集合、验收门、交付 commit、主 agent 结论、主线 commit、远端状态。缺任一关键字段，
状态不得进入 `integrated`。

`delivered` 不等于完成。只有同时满足以下条件才记为 `pushed`：

1. agent 交付 commit 的实际 diff 与写集合一致；
2. 主 agent 独立复核依赖和逻辑链，无未处置 A 级反例；
3. 聚焦、System Optimizer、全仓回归及 Ruff/diff 门按风险通过；
4. 由主 agent 合入 `system-optimizer-impl` 并确认远端 commit 一致。

## 当前依赖图

```text
O1 live A/B + evidence persistence (G3, pushed 2a3f0dd)
 ├─ dynamic replay verifier (D4-R4, integrated 1753cea)
 └─ dynamic CLI exception-safe recovery (G4, pushed 83a6b15)

S8/S9 contracts (existing)
 └─ executable L6c regression recovery (Z-L6C, pushed e70a85b)
     └─ L6c CLI lifecycle integration (G5, GPT assigned)

phase-gate audit
 └─ risk/change-count contract proposal (D5-R3, integrated draft e28153a)
     └─ implementation (blocked until user confirms semantics)
```

## 任务登记

| Task | Owner | 基线与 worktree | 依赖/写集合 | 状态与证据 |
|---|---|---|---|---|
| G3 O1 开销 A/B + 动态证据落盘 | GPT agent；glm5.3 验收 | 历史起点 `f46bc16`；agent worktree；主线接收于 `2a3f0dd` | `dynamic_collection.py`、对应 tests；依赖 O2 evidence 模型 | `pushed`。首个成功窗 disabled→enabled；后窗 enabled 并绑定首窗 overhead；原子 digest 文件+固定索引；当时聚焦 15、全仓 772、Ruff/diff 通过 |
| Z-L6C 可执行退化恢复 | glm5.3 | `system-optimizer-impl`；主线 `e70a85b` | `rollback/regression.py`、rollback schema/loader、tests；依赖 S8/S9 | `pushed`。只接受 S9 promoted last-good；显式 U_regression threshold；L1 精确恢复；失败 needs-attention；聚焦 33、System Optimizer 448、全仓 781、Ruff/diff 通过 |
| G4 dynamic-run 异常生命周期 | GPT agent；glm5.3 验收 | agent commit `5744c47`，主线基线 `e70a85b`；GPT `Looper-l4-fix/system-optimizer-pkg-b` | 仅 `cli.py`、`test_system_opt_dynamic_cli.py`；依赖 G3 persistence | `pushed` 为 `83a6b15`。异常后先恢复再持久化；恢复失败 needs-attention；lease 最终释放。聚焦 43、System Optimizer 451、全仓 784、Ruff/diff 通过 |
| D4 dynamic evidence replay | DeepSeek agent；glm5.3 验收 | 最终 agent commit `16bd3e4`，实际 parent `1767ae3`；主线 `1753cea`；`Looper-system-optimizer-deepseek-batch1` | `dynamic_replay.py` + tests；共享严格校验的 `collection_evidence_filename` | `pushed`。D4-R4 保留 forged-window/重复 probe 历史反例，补齐 O1 集合级共享语义、O2 probe→overhead 一对一、完整身份/孤儿/畸形/digest 校验；索引与 helper 均在读证据前拒绝路径穿越。聚焦 42、System Optimizer 478 通过；全仓 811 中仅既有 cloud token 时间碰撞失败，单测复跑通过；Ruff/diff 通过。远端主线已含 `1753cea` |
| D5 phase-gate 风险/单变更合同 | DeepSeek agent；glm5.3 验收 | 最终 agent commit `ce8f3e8`，实际 parent `16bd3e4`；主线 `e28153a` | 纯设计文档；未修改 phase-gate 实现 | `pushed-draft`。R3 采用 prepare→gate→execute 两阶段接口；risk 显式必填并绑定 manifest；plan digest 为计算属性；apply 后异常由 outcome/receipt 保留并计预算。preflight 计数、停类、风险冲突、receipt schema 四项仍待用户决定，确认前禁止编码。远端主线已含 `e28153a` |
| G5 L6c CLI 生命周期接线 | GPT agent | `Looper-l4-fix/system-optimizer-pkg-b`；最小任务合同基线 `e704b88`，交付前同步最新主线 | `services/api/looper_api/cli.py`、新/现有 L6c CLI tests；只读 `rollback/regression.py` | `assigned`。详见下节；不得碰 replay、phase-gate、dynamic_collection、DeepSeek 文件；agent 不 push/merge |
| Z-GOV-01 Agent 台账与状态对齐 | glm5.3 | `system-optimizer-impl@83a6b15` | governance/planning/architecture docs | `pushed`：规范本体 `e0da65e`，状态收口 `e704b88`，远端 `origin/system-optimizer-impl@e704b88`。docs-only，`git diff --check` 通过，未运行 pytest |
| Z-BASE-01 2026-08-24 实现重新基线 | glm5.3 | `system-optimizer-impl@e704b88` | planning/architecture docs；不碰 agent 代码写集合 | `integrated` 于 `da06607`，push 待本记录提交后一并执行。冻结动态纵向切片、证据/replay、G4、L6c 与 D4/D5/G5 的真实完成边界；docs-only，`git diff --check` 通过，未运行 pytest |

## G5 给 GPT 的正式任务合同

目标：为已经落地的 `rollback.regression.execute_regression_recovery()` 增加一个显式、
可故障注入的 CLI 生命周期入口；不重新实现 L6c 纯逻辑。

1. 开工三联自证后，确认最小任务合同基线
   `origin/system-optimizer-impl@e704b88`，并在交付前同步最新主线；报告
   实际 HEAD/parent，不把旧 commit 写成最新主线。
2. CLI 从版本化 `RegressionRecoveryRequest` JSON 读取 checkpoint、当前 S8 向量、显式
   threshold 和触发证据。不得提供 threshold、last-good、normalization 的默认值或推导。
3. 复用现有 manifest、state evidence、backend capability、lease/fencing、L1
   `SafetyController` 和 attention guard；不得建立第二套写配置路径。
4. triggered 时无论恢复成功或失败都持久化 request/outcome/rollback evidence；写入必须
   原子。恢复失败或证据持久化无法证明成功时 fail-closed；真实恢复失败必须标记
   needs-attention；所有路径最终释放 lease。
5. not-triggered 只能产评估证据，不写配置、不标 attention，并返回明确状态。
6. 故障注入至少覆盖：请求身份不一致、not-triggered 零写、恢复成功、L1 抛异常、
   non-kept/readback mismatch、attention 写入、证据写失败、lease 释放、原始异常上下文。
7. 保持模拟 backend 正向 E2E；local-linux 继续服从既有显式确认，不做云端修改。
8. 只提交任务文件，运行聚焦 L6c/CLI、安全/lease、System Optimizer 全量、仓库全量、
   Ruff 和 `git diff --check`。缺工具必须如实记录；不安装依赖；不 push/merge。

## D4-R 复验门

主 agent 验收 D4-R 时必须保留以下两个历史最小反例并确认转为拒绝：伪造索引窗口但
run 内 window identity 不变；重复 O2 probe digest。另需覆盖组件 overhead digest
互换、重复绑定、target/environment/collector 不一致、悬空/孤儿/畸形前缀文件。

索引自身携带可重算 digest 只能证明自洽，不能建立真实性根信任。当前 replay 合同只
承诺内部完整性与关联一致性；真实性以后必须由索引外 manifest、签名或可信锚提供。

## GitHub 代理记录

用户指定后续 GitHub 访问走端口 `65532`。2026-08-24 已用单次命令参数
`http.proxy=http://127.0.0.1:65532` 与 `https.proxy=http://127.0.0.1:65532` 成功 push
`83a6b15..e704b88`；未写持久 Git 配置。后续网络命令必须继续记录实际代理参数和远端
结果。
