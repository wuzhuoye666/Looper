# System Optimizer 实现重新基线（2026-08-24）

> 状态：current delivery baseline。
> 代码基线：`83a6b15`；治理文档基线：`e704b88`。
> 目的：冻结当前真实能力、未验收边界和并行依赖；不以文件数或测试数代替里程碑出口。

## 验证口径

- 最近一次代码全量验收发生在 `83a6b15`：System Optimizer 451/451、仓库
  784/784，Ruff 与 `git diff --check` 通过。
- `e0da65e`、`e704b88` 仅修改治理/架构文档，执行了 `git diff --check`，没有重跑
  pytest；因此不能把文档提交描述成新的 784 次代码回归。
- `.artifacts/pytest-g4-*` 是未跟踪测试临时目录，不是交付证据，也未进入 commit。

## 当前依赖和能力切片

| 区域 | 已落地并验收 | 尚未完成或禁止宣称 |
|---|---|---|
| L0–L2 安全与测量底座 | simulated/local-linux 后端、manifest/state evidence、lease/fencing、L1 snapshot→apply→verify→rollback、MeasurementBatch/digest | 腾讯云 CVM 未重新验收；ssh-remote、多节点、HTTP/审批/UI 不属于已验证能力 |
| L3/L4 压力与采集 | 五阶段压力合同、窗口化 builtin collector、压力产物 bundle 校验、O1/O2 活体回调 | 动态 O1/O2 在真实 CVM 的开销可接受性尚无任务阈值和实测裁决；O3 trace 仍属 M6+ |
| L5/L8 静态闭环 | 组件只建议不终裁、引擎统一 S0/S2/S7 裁决、组件隔离 incumbent、S8 六维/Pareto、S9 晋升合同 | 不把单组件 synthetic/阿里云结果外推到 CVM；环境内 ECDF/Z 和 E_m 未获准启用 |
| 动态 workload 纵向切片 | 外部负载合同与 session runner；O0 四解析器；多假设路由；O1/O2；业务复测、复验、门禁、重激活；CLI simulated E2E | 真实业务 workload 与 CVM 动态闭环未验收；声明式假设仍未由 O1 在线 S4 推导替换；L7 假设级负缓存仍 open |
| 动态采集开销与证据 | O2 相邻 disabled→enabled；O1 首个成功窗一次配对、后窗复用绑定；四类 digest 文件与固定索引落 `control/` | D4 初版 replay verifier 已拒绝，当前主线尚无获验收的离线回放器；固定索引不是真实性根信任 |
| 动态异常恢复 | G4 保证 dynamic-run 异常后先恢复，再持久化采集证据；恢复失败写 attention；最外层释放 lease | `phase-restoration.json` 的更广泛证据生命周期仍由后续包审计；不能将 CLI simulated 测试写成真机故障演练 |
| L6c 运行期退化恢复 | S8 `u_regression` + 任务显式 threshold；只接受 S9 promoted last-good 完整快照；经 L1 精确恢复，失败 needs-attention；rollback v1alpha2 + legacy loader；G5 CLI 已接入原子内容寻址证据图、固定索引、lease/attention 与故障生命周期 | 尚无真实目标运行期退化演练；threshold 数值不得跨任务默认 |

## 当前并行任务

- D4-R（DeepSeek）：修正动态证据 replay 的窗口、重复、组件映射和完整身份校验。
- D5-I1（DeepSeek，已验收）：risk quota 与 single-change 的两阶段纯合同、manifest
  风险解析和执行 receipt 模型已落地；D5-I2 的 dynamic-loop/backend/持久化接线未开始。
- G5（GPT 初交、glm5.3 接管修复/验收）：L6c CLI 生命周期、证据原子持久化、lease/attention/failure injection 已完成，主线修复提交 `6e19fb5`。
- Z-BASE-01（glm5.3）：本重新基线与当前文档状态对齐，不修改并行代码写集合。

具体 owner、worktree、基线和验收状态只在
[`agent-work-ledger-2026-08-24.md`](agent-work-ledger-2026-08-24.md) 更新，本文不建立第二套
任务状态源。

## 已确认的阻断或开放项

1. `risk_quota` 已有 manifest-bound 执行前风险解析和门禁 API，但 dynamic loop 尚未消费，
   `risky_interventions` 仍无生产者；D5-I2 必须按 receipt 的 `apply_started` 据实计数。
2. `single_change_per_window` 已有基于派生 `change_count` 的执行前拒绝 API，但动态循环
   仍未调用；当前“每窗一次”仍只是旧循环结构的隐式行为。
3. `identity_drift_action` 目前是只允许 `stop-phase` 的固定策略字段，不是可变行为选择；
   是否删除字段或保留为声明性固定策略需版本化提案。
4. O1 overhead 的墙钟来自并发 collector 集合，只证明集合成员关系，不支持单 collector
   开销归因。
5. 动态索引的自包含 digest 不能提供真实性；真实根信任需要索引外 manifest、签名或
   可信锚。

## 下一集成顺序

1. 先验收 D4-R，确保已落盘证据能够 fail-closed 回放；初版反例必须保留为负测。
2. G5 已验收；下一步只在具备授权与显式 threshold 的真实目标上演练 L6c，不把模拟门禁等同于真机结论。
3. D5-I1 已验收；下一包 D5-I2 负责 prepare→gate→execute、预算计数和 receipt 持久化，
   不得扩大到未确认的阈值或自动决策。
4. 三包稳定后再选择真实 Linux/CVM 动态会话与 L6c 故障演练；没有实测前成熟度不升级。
