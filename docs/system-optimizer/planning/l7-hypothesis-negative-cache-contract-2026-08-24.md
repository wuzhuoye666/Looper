# L7H-01：refuted hypothesis 第二类负缓存 schema 设计（R3）

> 状态：**R3 待主 agent 复审**；七项待确认已由用户拍板（§9 收敛为已冻结决策），schema 字段
> 待实施时定稿，不授权作者继续 L7H-02A/02B。
> 基线：`origin/system-optimizer-impl@4ed860c`。
> 归属：P1 L7H 链第一环（`unfinished-task-queue-2026-08-24.md` §3/§4 L7H-01）。
> 本文只冻结**设计候选与争议点**，不写实现；不授权作者继续 L7H-02A/02B。

---

## 0. R2/R3 相对 R1 的修订（主 agent 复审意见逐条）

| # | R1/R2 问题 | 修订 |
|---|---|---|
| 1 | 把 `o2_evidence_digest` 当"组件不是瓶颈"的反证 | **v1 仅允许"业务复测无改善"进入缓存**。`O2ComponentProbeEvidence`（`dynamic_collection.py:59`）只保存 hypothesis/采集窗口/快照，**无 verdict/阈值**，不构成反证裁决；O2 反证需另立 typed refutation-decision evidence（§4.1/§9 决策 1） |
| 2 | 孤儿证据规则自相矛盾（§6 fail-closed vs 测试矩阵第 9 项"保留"） | 明确选择：**v1 只校验 digest 格式，不验证证据存在性**（同 candidate cache 语义，真实性由索引外锚提供），删除"孤儿 fail-closed"（§6/§12） |
| 3 | `refute_kind` 来源一致性无法靠模型验证 | **删除 `refute_kind` 字段**（v1 单一来源，冗余）；来源区分待 typed evidence 落地后另议（§8/§12） |
| 4 | "无默认 TTL" 与"未注入则永久保留"冲突 | 明确未决：**retention policy 是 L7H-02B 的显式输入，无输入即不得实施持久化**；不写"永久保留"（§5/§9 决策 6） |
| 5 | L7H-02 写集合无法完成 refute→cache 运行时桥接 | 拆 **L7H-02A**（schema/混存分派/store/replay）与 **L7H-02B**（运行时 admission/bridge，依赖 02A + 字段决策）；identity 加 `metric_contract_digest` 与 `refutation_policy_digest`（§13/§3） |
| 6 | L7H-02B 写集合缺 `dynamic_loop.py`（R3） | **加入 `dynamic_loop.py`**：生产 `refute()` 调用在 `dynamic_loop.py:409/784`，`HypothesisLedger.refute()` 只有 hypothesis_id+digest，拿不到 workload/symptom/metric/policy 完整缓存身份，桥接必须在该调用点注入身份（§13） |
| 7 | 七项待确认未收敛（R3） | **用户已拍板**（§9 收敛为已冻结决策）：O2 v1 不支持、workload 用完整 contract、symptom 用 opaque、metric/policy 用完整 canonical digest、schema 用 hypothesis_semantics_version、retention 显式输入、并存方案 A |

---

## 1. 现状盘点（代码事实，重新 grep 核实）

### 1.1 现有 candidate negative cache（`negative_cache/__init__.py`）

| 项 | 现状 |
|---|---|
| schema | `looper.negative-cache-entry/v1alpha1`（`NegativeCacheEntry`） |
| identity | `NegativeCacheIdentity`：`environment_digest` + `candidate_parameters_digest` + `pressure_protocol_digest` + `formula_versions_digest`；`key = canonical_digest(四分量)` |
| verdict | `NegativeVerdict`：`NO_IMPROVEMENT_LCB` / `GATE_REJECTED`。注释明确**测量质量失败（S1.1 CV 门）不是候选裁决，永不缓存** |
| entry | `NegativeCacheEntry`：`schema_version` + `identity` + `metric_id` + `verdict` + `evidence_digests`（≥1、去重、sha256）+ `detail` + `recorded_at`；`digest` 覆盖全字段 |
| 存储 | JSONL append-only；`append_to` 读旧字节 → 拼一行 → `_atomic_replace_bytes`（`tempfile.mkstemp` + `fsync` + `os.replace`）→ 成功后 `self.add` 更新内存 |
| 加载 | `load` 逐行 `model_validate`，坏行抛 `ValueError`（不跳过） |
| 失效 | 无 TTL；身份 key 变化即 miss；**不验证 evidence_digest 指向的证据存在性** |

### 1.2 hypothesis refuted 状态与证据（`hypothesis.py`）

| 项 | 现状 |
|---|---|
| status | `PROPOSED → PROBING → CONFIRMED | REFUTED | SUPERSEDED` |
| refuted 记录 | `ComponentHypothesis.refute_evidence_digest`（单 digest）；`refute()` 只记录证据 digest，**不写缓存** |
| confirm 证据 | `InterventionExperiment`：`measurement_batch_digest` + `business_metric_id` + `accepted` + `business_lcb` |
| refute 来源 | `confirm()` 的 rejected 分支提示"call refute with the experiment batch digest"——**当前唯一有明确否定裁决的来源是业务复测无改善** |

### 1.3 O2 证据无反证裁决（R2 关键事实）

`O2ComponentProbeEvidence`（`dynamic_collection.py:59-83`）字段 = `schema_version` +
`hypothesis` + `observation_window_digest` + `collection_run` + `collection_overhead_evidence_digest`。
**没有 verdict、没有阈值、没有"组件不是瓶颈"的裁决**。D2 规则 2 明确 O2 只把假设推进到
`probing`，不产生任何终态裁决。因此 O2 证据 digest 不能当作"反证"。

### 1.4 业务复测裁决参数（供 refutation_policy 绑定）

`BusinessRetestPolicy`（`dynamic_adapters.py:282-296`）：`business_metric_id` / `phase_id` /
`scale` / `minimum_effect` / `minimum_samples` / `confidence_level` / `bootstrap_resamples` /
`random_seed` / `retest_window_count` / `window_wait_timeout_seconds` / `window_poll_seconds`。
这些是 S6/S7 裁决（`bootstrap_improvement`）的**任务显式输入**，无默认。

### 1.5 现有 identity 字段（供 hypothesis identity 复用）

| 维度 | 现有字段 / 来源 |
|---|---|
| environment | `capture_environment_fingerprint()`（`inventory.py:101`）→ `EnvironmentFingerprint` → `_current_environment_digest()` |
| workload | `WorkloadContract.digest`（`workload_contract_digest`）；`LoadCommandIdentity.identity_digest`（tool + argv_digest + declared_duration_seconds，不含 description） |
| component | `ConfigComponent`（cpu/memory/network/storage/numa） |
| symptom | `SymptomRecord`：`symptom_id` + `window_id` + `workload_contract_digest` + `evidence_digest` + `description` |
| formula/schema | `formula_versions_digest`（`Mapping[str,str]` 非空 → `canonical_digest`） |
| metric | `metric_id` / `MetricContract`（id/direction/aggregation/scale/minimum_effect…） |
| refutation policy | `BusinessRetestPolicy`（scale/minimum_effect/confidence_level/bootstrap_resamples…） |

---

## 2. 并存方案（candidate cache 与 hypothesis cache）

| 方案 | 描述 | 优点 | 缺点 |
|---|---|---|---|
| **A. 同一 JSONL + schema 分派（推荐）** | 新 schema `looper.hypothesis-negative-cache-entry/v1alpha1`，与 candidate 共用同一 JSONL；`load` 读行看 `schema_version` 分派 | 复用 append-only/原子替换/坏行报错；不建第二套写入；迁移最简 | `load` 需版本分派；两类条目排序需确定性 |
| B. 分文件 / 分 schema | 两类各一个 JSONL | 完全隔离 | 第二套路径/管理；跨文件查询 |
| C. 同一 schema + `entry_type` | 单模型加字段 | 单模型 | 破坏 v1alpha1 兼容（旧行无 entry_type） |

**已冻结：方案 A**（用户 2026-08-24 拍板）。

---

## 3. hypothesis identity 字段（组合键，用户已拍板口径）

identity 是**组合键**（多字段 canonical digest），**不预设任何单一字段为主键**。字段（口径已由
用户拍板，见 §9）：

| 字段 | 类型 | 来源 | 口径（已冻结） |
|---|---|---|---|
| `environment_digest` | sha256 | `_current_environment_digest()` | 环境变化即失效 |
| `workload_identity_digest` | sha256 | **完整 `WorkloadContract.digest`** | 绑定完整 workload 合同（含 objective/slo/gate），不降级为 load identity |
| `component` | `ConfigComponent` | `ComponentHypothesis.component` | 组件变化即失效 |
| `symptom_class_digest` | sha256 | **opaque digest（暂保留）** | 等结构化 symptom schema 落地后再定义；**禁止用窗口 ID 或自由文本直接散列** |
| `metric_contract_digest` | sha256 | **完整 `MetricContract` canonical digest** | 不同业务指标不得共用缓存 |
| `refutation_policy_digest` | sha256 | **完整 `BusinessRetestPolicy` canonical digest** | 不同反证阈值不得共用缓存 |
| `formula_versions_digest` | sha256 | 决策链公式版本 | 公式版本变化即失效 |
| `hypothesis_semantics_version` | 字面量（**必填**） | 假设语义版本 | 不绑定 proposal v1/v2（只增风险字段，不绑）；必填，语义变化即失效 |

> `metric_contract_digest` + `refutation_policy_digest` 防"不同业务指标 / 不同反证阈值错误共用
> 缓存"；`hypothesis_semantics_version` 必填但不绑定 proposal 版本（避免把只增风险字段的 v1/v2
> 差异当语义变化）。

---

## 4. refuted 准入边界

### 4.1 可缓存为 refuted（v1 极窄：仅业务复测无改善）

| 情形 | 证据 | 前置条件 |
|---|---|---|
| 合格业务复测无改善 | `InterventionExperiment.accepted=False` 的 `measurement_batch_digest` | 复测身份与基线可比（S0）、样本充足、无身份漂移、测量稳定 |

> **O2 反证在 v1 不可缓存**：`O2ComponentProbeEvidence` 无反证裁决（§1.3）。若未来引入带
> 策略/阈值/公式版本的 typed refutation-decision evidence（§9 问题 1），才可作为第二来源。

### 4.2 绝对不可缓存为 refuted（红线）

- **数据不足**（样本不足、置信度不足）。
- **身份漂移**（workload 身份漂移，复测不可比）。
- **采集失败**（O1/O2 采集失败、观测缺失）。
- **测量不稳定**（S1.1 CV 稳定性门失败——测量质量问题，不是假设证伪）。
- **门禁拒绝**（single-change / risk-quota 执行前拒绝——intervention 未执行）。
- **intervention 未执行**（preflight 拒绝、apply 前失败、零写 REJECTED）。
- **安全回滚失败**（needs-attention、机器状态不确定）。

> 核心原则：**只有"该假设被明确否定"的证据可缓存**；"没测到""测不稳""没敢测""测出事故"
> 都不等于 refuted。被拒 intervention ≠ refuted hypothesis。

---

## 5. 失效规则

- 任一身份字段变化 → 查询 miss（跨身份不匹配，无模糊匹配）。
- **retention policy 是 L7H-02B 的显式输入**：无用户提供的保留期/失效策略，**不得实施持久化**；
  不写"永久保留"，不写任何默认 TTL。TTL/保留期保持**明确未决**（§9 问题 6）。

---

## 6. 证据与 replay

- digest：strict lowercase `sha256:<64hex>`。
- 内容寻址：`HypothesisNegativeCacheEntry.digest` 覆盖全字段，可从原始字节重算。
- `evidence_digests`（≥1、去重、sha256）绑定 refute 证据。
- **只校验 digest 格式，不验证证据存在性**（同 candidate cache 语义）：证据可能分布在 receipt
  store / `control/` / 会话目录等多个 store，v1 无统一 resolver，不承诺存在性校验；真实性由
  索引外 manifest/签名提供。孤儿/悬空 digest **不自动判定、不 fail-closed**（诚实声明）。
- 坏行 / 篡改 entry 内容 / 重复 evidence digest / 冲突：`load` 逐行校验，任何非法即 fail-closed
  （抛 `ValueError`，不跳过）。

---

## 7. 原子发布（沿用现有语义，不新设计）

- 沿用 candidate 的 `append_to`：读旧字节 → 拼一行 → `_atomic_replace_bytes`
  （`tempfile.mkstemp` + `fsync` + `os.replace`）→ 成功后更新内存索引。
- **不重新设计第二套写入协议**；唯一新增是"按 schema_version 分派模型"与"组合键 identity"。

---

## 8. schema 草案（全字段表，口径已由用户拍板）

```
HypothesisNegativeCacheSchema = "looper.hypothesis-negative-cache-entry/v1alpha1"

HypothesisNegativeCacheIdentity:
  environment_digest:          sha256
  workload_identity_digest:    sha256        # = 完整 WorkloadContract.digest
  component:                   ConfigComponent
  symptom_class_digest:        sha256        # opaque，等结构化 symptom schema 落地
  metric_contract_digest:      sha256        # = 完整 MetricContract canonical digest
  refutation_policy_digest:    sha256        # = 完整 BusinessRetestPolicy canonical digest
  formula_versions_digest:     sha256
  hypothesis_semantics_version: 字面量       # 必填；不绑定 proposal v1/v2

HypothesisNegativeCacheEntry:
  schema_version:   Literal[上述 schema]
  identity:         HypothesisNegativeCacheIdentity
  evidence_digests: list[sha256]   # ≥1、去重；v1 语义 = 业务复测无改善的 measurement_batch_digest
  detail:           str            # 1..1000
  recorded_at:      datetime

  digest: canonical_digest(model_dump(mode="json"))
  key:    canonical_digest(identity.model_dump(mode="json"))
```

> **无 `refute_kind` 字段**（R2 删除）：v1 只有"业务复测无改善"一种来源，来源区分待 typed
> refutation-decision evidence 落地后另议。

---

## 9. 已冻结决策（用户 2026-08-24 拍板）

1. **O2 反证**：v1 暂不支持；未来另建带策略/阈值/公式版本的 typed refutation-decision evidence
   作为第二 refuted 来源。
2. **workload identity**：使用**完整 `WorkloadContract.digest`**（不降级为 load identity）。
3. **symptom class**：暂保留 **opaque digest**；等结构化 symptom schema 落地后再定义，**禁止用
   窗口 ID 或自由文本直接散列**。
4. **metric/policy**：`metric_contract_digest` 绑定**完整 `MetricContract`** canonical digest；
   `refutation_policy_digest` 绑定**完整 `BusinessRetestPolicy`** canonical digest。
5. **schema 版本**：使用**必填的 `hypothesis_semantics_version`**；不绑定 proposal v1/v2（只增
   风险字段，不构成语义变化）。
6. **retention**：任务必须**显式提供**保留期/失效策略；未提供时**禁止运行时持久化**。
7. **并存**：采用**方案 A**——同一 JSONL + 独立 schema 分派。

---

## 10. 状态迁移表（假设状态 → 是否入缓存）

| 假设终态 | 证据 | 是否入 hypothesis cache | 说明 |
|---|---|---|---|
| `CONFIRMED` | accepted 业务复测 | 否（正向结论） | 不入负缓存 |
| `REFUTED`（业务复测无改善） | `measurement_batch_digest`（accepted=False） | **是** | 身份可比、样本充足、无漂移、测量稳定 |
| `SUPERSEDED`（被 confirmed 顶替） | — | **否** | 是被"确认"顶替，不是被"证伪" |
| `PROPOSED/PROBING`（未终态） | — | 否 | 非终态不缓存 |
| O2 反证（暂无 typed evidence） | — | **否（v1）** | O2 无裁决；待 typed evidence 后另议 |
| 测量不稳定 / 门禁拒绝 / 采集失败 / 身份漂移 / 回滚失败 | — | **否（红线）** | §4.2 |

---

## 11. 兼容矩阵

| 迁移场景 | 策略 |
|---|---|
| 旧 candidate JSONL（无 hypothesis 行） | 不迁移；`load` 按 schema_version 分派 |
| 新 hypothesis 行混入同一 JSONL | `load` 分派；两类 entry 各自原子追加 |
| 旧 JSONL truncated 尾行 | fail-closed（`append_to` 抛 `ValueError`） |
| v1alpha1 candidate entry 字段冻结 | 不加字段、不回填默认值 |

---

## 12. 负向测试矩阵（L7H-02A 实施时验收）

| # | 用例 | 预期 |
|---|---|---|
| 1 | 同身份 refuted 命中 | 返回该 entry |
| 2 | 任一身份分量变化 | miss |
| 3 | 不同 environment / component / symptom class | miss |
| 4 | 不同 metric_contract 或 refutation_policy | miss（R2 新增） |
| 5 | 公式版本变化 | miss |
| 6 | 坏行（非法 JSON / 缺字段） | `load` fail-closed，不跳过 |
| 7 | 篡改 entry 内容（digest 不匹配） | fail-closed |
| 8 | 重复 evidence digest | 模型校验拒绝 |
| 9 | 孤儿 evidence digest（无对应证据文件） | 不自动判定、不 fail-closed（只校验格式，诚实声明） |
| 10 | 测量不稳定 / 门禁拒绝 / 回滚失败被写入 | **拒绝**（准入门禁，L7H-02B） |
| 11 | append 失败（磁盘写失败） | 旧文件与内存索引不变（沿用 candidate 语义） |
| 12 | 与 candidate entry 混存 | 各自分派正确、互不误读 |

---

## 13. L7H-02 拆包（写集合，本文不实现）

### L7H-02A：schema、混存分派、store/replay

- 写集合：`packages/core/looper_core/system_opt/negative_cache/__init__.py`（新增
  `HypothesisNegativeCacheIdentity` / `HypothesisNegativeCacheEntry` / schema 分派 load）+
  新增 `tests/test_system_opt_hypothesis_negative_cache.py`（§12 的 1-9、11-12 项）。
- 不可改：`hypothesis.py`、`dynamic_adapters.py`、`dynamic_loop.py`、`cli.py`、任何现有测试、
  ledger/backlog、receipt/CLI/dynamic loop。

### L7H-02B：运行时 admission/bridge（依赖 L7H-02A + 字段决策）

- 写集合：`dynamic_loop.py`（在 `refute()` 调用点 `dynamic_loop.py:409/784` 注入完整缓存身份
  environment/workload/component/symptom/metric/policy/formula，或改 `refute()` 签名由调用方传入）+
  `hypothesis.py`（`refute()` 到 cache 的桥接，或新增 bridge 模块）+ `dynamic_adapters.py`
  （admission 判定：只准入"身份可比+样本充足+测量稳定的业务复测无改善"）+ 对应测试。
- 前置：§9 字段决策 + retention policy 显式输入。
- 关键：`HypothesisLedger.refute(hypothesis_id, evidence_digest)` 当前只有 hypothesis id + 证据
  digest，**拿不到 workload/symptom/metric/policy 完整缓存身份**，仅改 `hypothesis.py` +
  `dynamic_adapters.py` 无法可靠接线；必须在 `dynamic_loop.py` 调用点注入身份。
- **本文不授权实施 L7H-02A/02B**。

---

## 14. 引用校验记录

- `negative_cache/__init__.py`：`NegativeCacheIdentity` 38-46、`NegativeCacheEntry` 49-71、
  `NegativeVerdict` 30-36、`append_to` 161-174、`load` 176-189。
- `hypothesis.py`：`HypothesisStatus` 36-41、`ComponentHypothesis` 79-92（`refute_evidence_digest`
  88）、`refute()` 224-237、`confirm()` 195-222（rejected 分支提示 refute）。
- `dynamic_collection.py`：`O2ComponentProbeEvidence` 59-83（无 verdict/阈值）。
- `dynamic_adapters.py`：`BusinessRetestPolicy` 282-296、`HypothesisProposal`/`HypothesisProposalV2`。
- `workload.py`：`LoadCommandIdentity.identity_digest` 69-83、`WorkloadContract.digest` 169-171。
- `inventory.py`：`capture_environment_fingerprint` 101。
- `decision-log.md` SO-D019：L7 第二条目类型 schema 并存细节留待提案。
