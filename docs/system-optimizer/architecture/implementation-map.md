# 实现地图：架构概念 → 代码对照（2026-08-23 深夜版）

> 用途：架构提出者对照代码快速建立完整实现理解；答辩前自查。
> 所有路径相对 `packages/core/looper_core/system_opt/`（另有标注者除外）。
> 配套阅读：`architecture/overall.md`（架构 v2）、`contracts/formula-provenance.md`（公式登记表）。

## 建议阅读路径（明天并行用）

- **30 分钟版**：本文 §九层对照表 → §公式总线 → §静态数据流 → §动态六构件。
- **2 小时版**：再加 §关键测试（读测试是理解语义最快的方式）+ 挑 `engine/loop.py`
  和 `dynamic_loop.py` 通读（两个编排器是全部设计决策的汇合点）。

## 一、九层 → 文件对照

| 层 | 文件 | 关键符号 | 实际做什么 |
|---|---|---|---|
| L0 执行后端 | `executor/simulated.py` `executor/local_linux.py` `executor/runner.py` | SimulatedBackend / LocalLinuxBackend / SubprocessCommandRunner | 真实后端受可执行白名单 + 可写根双重约束；runner 是所有子进程的唯一出口 |
| L1 配置安全底座 | `config_manifest.py` `state_evidence.py` `safety.py` `lease.py` | ConfigManifest/ConfigItem；ConfigurationStateEvidence/authorize；SafetyController；FileTargetGuard | manifest 声明合法域 → 状态证据定所有权（未知即拒绝改）→ 安全控制器 snapshot→apply→verify→rollback → 单写者租约 + fencing token |
| L2 测量合同 | `scoring.py`（MeasurementBatch/MetricEvidence/GateEvidence） | digest 属性 | 统一包装；**digest 双范围**：`primary_metric_evidence_digest`（单指标 canonical）vs `measurement_batch_digest`（整批） |
| L3 压力器 | `pressure/__init__.py` | StandardPressureProtocol（五阶段 prepare/warmup/measure/verify/cleanup）；PhasedPressureMeasurementAdapter（现役耦合路径）；PhasedPressureCollectionAdapter（PKG-B 解耦路径：measure 只产 PressureExecutionEvidence+ZIP bundle，采集走 L4） | 阶段合同 fail-closed：任何阶段失败 cleanup 必跑 |
| L4 采集器 | `collector.py` `interference.py` | BuiltinLinuxGuestCollector（**已窗口化** begin_collection/finish/cancel）；verify_collection_artifact_bundle（ZIP 逐文件 sha256，容器字节不作身份）；干扰检查 | /proc /sys 微指标 + 压力工具产物解析；采集开销 A/B 证据（成对裸墙钟，无阈值） |
| L5 组件优化器 | `component/__init__.py` `component/mapping.py` `component/strategy.py` + `strategies/*.yaml` | ComponentOptimizer（suggest_candidates/candidate_pool/run）；StrategyFormulaMapping（when 条件 bootstrap 置信 + 域校验） | **只建议不终裁**；越域建议拒绝并留痕（formula_rejections） |
| L6 回退器 | `rollback/__init__.py` | 四级：候选级（每候选测完即回退，真机验证过）/ 相位级 / 退化级（依赖 S8 U_regression）/ 崩溃级 | verify_phase_restoration 三态 |
| L7 负缓存 | `negative_cache/__init__.py` | NegativeCacheEntry，身份 = 环境×候选参数×协议×公式版本 四 digest | append-only；调度器开轮前查表跳过已证无效；红线：缓存证据不是结论 |
| L8 总引擎 | `engine/{scorer,judge,scheduler,incumbent,loop}.py` + `tuning.py` | run_engine_loop；evaluate_candidate（S0→S2→S7 固定序） | 只做调度/判断/打分三件事；SO-D017 预筛 tracker **按组件隔离**（SO-D018）；GPT 修复后 scheduler 选中候选被 L5 精确定向执行（身份违规即 raise） |

## 二、公式总线 S0–S10 → 实现位置

| 公式 | 实现 | 备注 |
|---|---|---|
| S0 可比性 | `scoring.comparable()` | 身份字段逐项比对 |
| S1/S1.1 校准+CV 门 | CLI `calibrate-pressure` / `derive-pressure-gate` | 门 = 单侧 95% bootstrap 上界，target-local（今天两台机器两扇门：4.64% / 12.57%） |
| S2 硬门禁 | `scoring.evaluate_hard_gates` | 不可被收益补偿 |
| S3 路由 | 静态：tuning `routed_components`；动态：`hypothesis.py`（≥2 竞争假设才许干预） | |
| S4 二维优先级 | `scoring.diagnostic_priorities` + F-PROJECT-002 `pressure_value`/`adverse_change`（显式 scale，方向-方法相容表在 policy 校验） | 样本充足度准入已落地（eligible_metric_ids）；E_m 完整版守提案门 |
| S5 合法搜索域 | `domain.resolve_domain` | 能力∩授权 |
| S6/S7 改善+接受 | `scoring.bootstrap_improvement`（F-PROJECT-S6-S7/v1alpha1） | **LCB>MDE 严格大于**；黄金数值被特征测试钉死 |
| S8 结果向量 | `result_vector.py` | 六维 + pareto_layers + PromotionContract |
| S9 晋升复验 | `result_vector.evaluate_promotion` + `verification.py`（复验观测生产者，passed 来自重测 S7、可为 false——M11 已闭合） | min_distinct_time_blocks 使复验窗成为结构必需 |
| S10 停止 | 静态：tuning StopReason；动态：`phase_gate.py` DynamicPhaseGateContract（五类停止字段化 + 防振荡） | |

## 三、静态相位一次 run 的实际数据流

```
状态采集(state-inventory) → [所有权未知? authorize-state 操作员授权]
→ calibrate-pressure（report-only 校准批次）
→ derive-pressure-gate（冻结批次→bootstrap 上界=门）
→ run：
  租约 acquire → 基线测量（必须过稳定门）
  → 逐候选（公式建议优先+搜索兜底，域校验，负缓存排除，基线镜像过滤）
  → 安全施加→测量→回滚（每个候选！）
  → S0→S2→S7 裁决 → 无改善写 L7
  → 显式停止 → 相位回退验证（读回=基线）
```

真机三道纪律（今天实测）：所有权授权 / 内核兼容下限 / **门绑定基线状态**（never 态 CV 9.9% vs madvise 态 3.5%——门不跨状态复用）。

## 四、动态相位六构件 + 编排

| 构件 | 文件 | 核心约束 |
|---|---|---|
| workload 合同 | `workload.py` | load_provider=external-test 唯一枚举；argv 只存 digest（SO-D020：引擎永不造负载） |
| 观察窗口 | `observation.py` | O0 解析器注册表（stress-ng YAML/fio/iperf3 JSON，真实夹具钉数值）；身份漂移即 WorkloadIdentityDrift |
| 假设路由 | `hypothesis.py` | D2 三硬规则；confirmed 唯一走 accepted 业务复测；L7 桥接待提案 |
| 结束门禁 | `phase_gate.py` | 判定顺序：安全→身份→预算→目标→收敛；决策必须引用触发字段 |
| 复验窗口 | `verification.py` | RetestOutcome = 改善 + 原始批次 digest |
| 重激活 | `reactivation.py` | A+B 案；资格≠自动重启 |
| **编排器** | `dynamic_loop.py` | run_dynamic_phase：每窗 组装→SLO/症状→账本→干预(注入)→复验→门禁判定→显式停止；负载/施加/测量全注入回调 |

## 五、证据与纪律机制（答辩硬货）

- **digest 三层**：单指标 canonical / 整批 / 运行记录——所有结论可从原始字节重算；
- **fail-closed 点**：所有权未知、越域、缺 scale、负利用率、非有限值（模型入口）、样本不足（不进路由）、身份漂移、相位恢复失败（phase-restoration-failed 停止而非 completed）、干扰窗口（进程模式）、稳定性超门、候选身份违规（L5 执行≠选中即 raise）；
- **台账纪律**：云端命令逐条 sha256 落 ndjson（N0001-N0032 / C0001-C0024 两套范本）。

## 六、关键测试（理解语义的最快入口）

| 文件 | 钉死了什么 |
|---|---|
| `tests/test_system_opt_verdict_characterization.py` | 引擎-组件裁决一致性不变式 + 安全语义分歧 + S6/S7 黄金数值（seed7: 2.05/1.65/2.475） |
| `tests/test_system_opt_optimization_run_versions.py` | schema 分派加载（v1alpha1 双形态/legacy 不回填）+ 遗留工件 digest 锚 |
| `tests/test_system_opt_dynamic_loop.py` | 动态相位 7 场景端到端（含晋升 fail-closed、漂移停相） |
| `tests/test_system_opt_policy_scoring.py` | fail-closed 族 + F-PROJECT-002 变换族 + 恒等式属性测试 |
| `tests/test_system_opt_engine_composition.py` | GPT 候选身份：选中=执行，越域留痕不执行，非恢复相位不得 completed |
