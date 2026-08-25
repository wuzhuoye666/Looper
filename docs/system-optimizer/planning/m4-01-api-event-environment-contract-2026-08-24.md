# M4-01 API、事件投影与 EnvironmentSnapshot 合同

> 状态：draft design；未开放真实写 API。
> 原则：先只读投影，写操作必须等待 M4-02 权限与人工审批合同。

## 1. 当前代码事实

- core 已有版本化 `EnvironmentSnapshot(v1alpha1)`，覆盖 CPU、NUMA、内存、磁盘、NIC、
  accelerator、软件栈和 synthetic 标记。
- API 的 `TargetRecord` 仍保存无类型 `inventory_json`、`fingerprint_json` 和旧
  `snapshot_digest`；该 digest 的历史 payload 口径不止一种。
- `/api/v1/targets` 已有同步、导入、连接、SSH test 和销毁接口，但没有 System Optimizer
  operation/evidence/readiness 资源。
- `EventRecord` 是通用 append 记录，以 `experiment_id` 查询最大 sequence 后加一；当前没有
  System Optimizer event envelope，也没有并发 sequence 唯一约束。
- 真实执行仍由 CLI 的 lease、fencing、attention、receipt 和 L1 safety 路径控制。

## 2. 最小资源模型

M4-01 不覆盖旧字段。建议新增并双写：

| 资源 | 最小字段 | 兼容规则 |
|---|---|---|
| EnvironmentSnapshotRecord | target、schema、typed payload、digest、collected_at、source evidence | 旧 snapshot_digest 原样冻结；新 digest 单独命名 |
| SystemOptOperation | operation/execution/target、mode、state、request digest、latest receipt、attention | 只投影已有证据，不成为第二状态机 |
| SystemOptEvidenceRef | kind、digest、filename/CAS locator、operation、created_at | 内容寻址；API 不重写证据正文 |
| SystemOptEvent | operation-local sequence、event type、receipt/evidence digest、created_at | 从 durable receipt/evidence 投影，可幂等重建 |

typed 双写的顺序固定为：构造并验证 `EnvironmentSnapshot` → 持久化 typed record → 更新
目标的 typed pointer；旧 `fingerprint_json`/`snapshot_digest` 继续按原逻辑写。任何一侧失败
都不得声称两个 digest 等价。迁移期间读者按 schema 显式分派，禁止把旧 JSON 自动填成
typed snapshot。

## 3. API 分阶段

### M4-01A：可立即实现的只读面

- `GET /api/v1/targets/{target_id}/system-optimizer/readiness`
- `GET /api/v1/system-optimizer/operations/{operation_id}`
- `GET /api/v1/system-optimizer/operations/{operation_id}/events`
- `GET /api/v1/system-optimizer/operations/{operation_id}/evidence`
- `GET /api/v1/targets/{target_id}/environment-snapshots/{digest}`

响应只投影已经落盘并验证的状态；receipt 链、attention 或 evidence 图损坏时返回明确的
fail-closed 状态，不能跳过坏记录继续显示“成功”。分页、保留期和 event sequence 的并发
生成方式仍需实现前逐字段冻结，当前不设置默认值。

### M4-02 后才允许的写面

prepare、approve、execute、recover、clear-attention 均属于真实状态变更。它们必须复用现有
lease/fencing/receipt/L1 路径，并绑定操作者、审批、幂等键和请求 digest；API handler 不得
直接调用 backend 写配置。M4-01 不实现这些端点。

## 4. 事件真实性边界

durable receipt 和 evidence graph 是事实源，事件是可重建投影。候选、恢复、回滚、
needs-attention、证据发布各有独立 event type，payload 只保存摘要引用和非敏感展示字段。
事件丢失可从事实源重建；事实源损坏不能靠事件补真。现有 `max(sequence)+1` 在并发下可能
重复，System Optimizer 事件不能直接照搬，需 operation-local 原子序列或内容确定性序号。

## 5. 实施顺序与验收

1. 冻结 typed DB migration、旧/新 digest 命名和 rollback。
2. 实现 snapshot 双写及 round-trip 测试，旧 API golden payload 不漂移。
3. 实现 operation/evidence/event 的只读投影和损坏 fail-closed 测试。
4. 完成 M4-02 权限、审批和审计后，才设计写 API。

验收必须覆盖迁移前数据库、双写一侧失败、未知 schema、并发事件、证据篡改、attention
阻断和 API 重试幂等。远程目标、多节点和 UI 分别留给 M4-03/M4-04。
