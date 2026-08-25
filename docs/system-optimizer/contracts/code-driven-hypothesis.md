# 代码驱动性能假设与容量裁决契约

> 状态：core contract implemented / orchestration not connected
> 日期：2026-08-24
> 适用范围：受控 workload 优化任务；不是生产常驻自治调参。

## 来源与定位

本契约吸收 SysInsight 论文《Why Database Manuals Are Not Enough: Efficient and
Reliable Configuration Tuning for DBMSs via Code-Driven LLM Agents》的“源码影响路径 →
性能假设 → 实验验证”结构。来源：PVLDB 19(6), 2026，arXiv:2603.22708v1，
DOI:10.14778/3797919.3797940。

Looper 不复制论文系统，也不把论文中的 MySQL 收敛速度或性能提升外推到 Linux guest。
本仓库实现是独立的项目合同：代码和配置证据只生成候选，最终结论只能来自 Looper 自己的
SLO 容量测试与安全回滚证据。

## 当前实现边界

本轮只实现三件事：

1. `OptimizationHypothesis`：把运行时证据、源码或配置合同、上下文和候选绑定为不可变摘要。
2. `rank_authorized_hypotheses`：只在目标实测能力域与任务授权域中排序候选，不生成参数值。
3. `evaluate_capacity_frontiers`：比较基线与候选的 `committed_tps` 容量区间，只有最坏情况
   收益越过显式最小收益且回滚验证成功时才接受。

当前不做：

- 不接入 LLM 在线生成参数值。
- 不实现 LLVM 全程序污点分析、SHAP、关联规则挖掘或因果概率。
- 不新增 API、数据库表、前端页面或后台常驻任务。
- 不修改容量测试任务正在开发的服务层合同。
- 不把历史规则当作当前环境事实，也不跳过真实容量复测。
- 不自动 keep 候选；候选测量后仍由现有安全控制器恢复实际 snapshot。

## 复用现有能力

| 子问题 | 复用入口 | 本轮新增内容 |
|---|---|---|
| 原始与标准化证据 | `looper_core.evidence.EvidenceManifest`、CAS digest | 假设直接引用证据 digest |
| 配置事实和动态合法域 | `ConfigManifest`、`ResolvedDomain` | 候选越权或越界直接拒绝 |
| 运行时诊断顺序 | `DiagnosticPriority` | 不再创造第二套加权总分 |
| 安全施加与回滚 | `SafetyController` | 容量接受要求 `rollback_verified=true` |
| 业务容量 | 容量报告的 resolved frontier | 增加区间比较公式和不可比门禁 |

## 数据流

```text
容量边界附近的 runtime profile digest
              +
源码行/符号或配置合同 digest
              │
              ▼
      OptimizationHypothesis
              │
     context / domain / component gate
              │
              ▼
      已授权候选的确定性顺序
              │
    SafetyController: snapshot/apply/verify
              │
              ▼
      相同容量协议重新测量
              │
              ▼
   evaluate_capacity_frontiers
              │
              ├── accepted
              ├── rejected
              ├── inconclusive
              ├── incomparable
              └── safety-failed
```

## 假设证据门

每条假设至少包含：

- 一个 `runtime-profile` digest；
- 一个 `source-code` 或 `configuration-contract` digest；
- 完整的上下文 digest；
- 受影响组件；
- 一个或多个明确候选参数。

`source-code` 证据必须绑定符号或精确行范围。重复引用同一 digest 会被拒绝，避免用同一份
证据虚增支持度。LLM 文本本身不是证据；它只能形成 `statement`，并引用已经固化的证据。

状态只允许：

- `observed-association`
- `supported-hypothesis`
- `intervention-supported`
- `unresolved`

升级到 `intervention-supported` 必须产生新文档，引用前一版本 digest 和容量结果 digest；
不能原地改写旧假设。

## 候选排序门

排序器只接受已经由 `resolve_domain` 求交得到的动态域。每个候选必须同时满足：

1. 假设上下文与当前任务完全一致。
2. 参数存在于当前任务授权域。
3. 参数值在目标实测能力域、清单声明域和任务授权域的交集中。
4. 参数所属组件被假设明确解释。
5. 该组件存在当前运行时诊断证据。

排序沿用 `DiagnosticPriority` 的 Pareto 层和既有稳定排序，再以假设成熟度和 digest 打破
平局。没有新建“因果置信分”，也没有用证据条数充当概率。拒绝项返回稳定原因，调用方不得
在空结果时静默退回 LLM 猜测。

## 容量区间裁决

容量测试给出真实容量所在区间：

```text
[confirmed_pass, confirmed_fail]
```

令基线为 `[Bpass, Bfail]`，候选为 `[Cpass, Cfail]`：

```text
point = midpoint(candidate) / midpoint(baseline) - 1
lower = Cpass / Bfail - 1
upper = Cfail / Bpass - 1
```

公式 ID 为 `F-CAPACITY-FRONTIER-001/v1alpha1`。只有：

```text
lower > explicit_minimum_effect AND rollback_verified
```

才返回 `accepted`。`upper <= explicit_minimum_effect` 返回 `rejected`；区间跨越最小收益边界
返回 `inconclusive`。任何上下文差异返回 `incomparable`，回滚未验证返回
`safety-failed`。

最小收益没有默认值。调用方必须提交经当前任务校准和确认的值。

## 强制身份

容量基线与候选必须逐项一致：

- `source_digest`
- `workload_digest`
- `slo_digest`
- `environment_digest`
- `network`
- `target_id`
- `capacity_unit`
- `confidence_level`
- `measurement_contract_digest`

这组字段使代码、场景、SLO、机器、链路和测量合同任一漂移都能 fail closed。跨机器或跨
场景经验只能生成新的低成熟度假设，不能进入本次容量比较。

## 失败模式与测试

| 失败模式 | 处理 | 测试 |
|---|---|---|
| 缺 runtime 或源码/配置来源 | 模型拒绝 | `test_hypothesis_requires_*` |
| 源码证据没有行或符号 | 模型拒绝 | `test_source_code_evidence_*` |
| 参数未授权或值超出动态域 | 候选拒绝并返回原因 | `test_ranker_uses_*` |
| 代码、SLO、环境或链路漂移 | `incomparable` | `test_capacity_frontier_fails_closed_*` |
| 容量区间重叠 | `inconclusive` | `test_capacity_frontier_distinguishes_*` |
| 回滚无法验证 | `safety-failed` | `test_capacity_frontier_fails_closed_*` |
| 容量上下界非法或身份缺失 | 抛出 `InsufficientEvidence` | `test_capacity_frontier_requires_*` |

## 后续集成门

容量任务的服务层合同稳定后，适配器只做字段归一和 digest 绑定：

```text
CapacityStudy report
  → target frontier
  → capacity identity
  → evaluate_capacity_frontiers
  → capacity decision artifact
  → 新 hypothesis revision
```

在完成该适配器、EvidenceManifest/CAS 持久化和离线 replay 前，不宣称论文方案已经形成
端到端产品闭环。
