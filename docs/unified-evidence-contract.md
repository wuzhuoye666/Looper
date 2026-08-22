# 统一实验与 Trace 数据底座（阶段 1）

本文描述 Looper 的统一 Evidence Contract：不同 Benchmark 的原始输出如何经由 Adapter 进入同一个可验证、可追溯的数据模型，以及后续分析如何在不重新运行 Benchmark 的情况下离线重算指标。

## 1. 统一底座解决什么问题

不同 Benchmark 的原始输出格式互不兼容：

- CCL-Bench 输出 Workload Card、GPU Trace 和启动脚本；
- BenchBase 输出 summary、transaction histogram、raw latency；
- DCPerf 输出 Benchpress result；
- 后续还会接入其他测试套件。

如果让分析模块、优化器和 Web/API 直接解析这些上游格式，每个新 Benchmark 都要在下游复制一遍解析逻辑，且上游格式一变全部失效，更无法回答"这个结论是从哪份原始数据、哪个工具版本算出来的"。

统一底座的目标数据流：

```
不同 Benchmark 原始输出
    → 对应 Benchmark Adapter（唯一允许接触上游格式的地方）
    → Looper 统一 Evidence Contract
    → 统一存储与校验（CAS digest + JSON Schema + Pydantic）
    → 数据分析模块（含 Trace Evaluator）
    → 优化决策或测试结论
```

统一的是包装格式、身份、单位、来源和证据关系，不是业务含义：SmallBank 的 TPS 和 All-Reduce 的 completion time 各自保持原语义，只是都被包进同一种 `MetricObservation` 结构。

## 2. 三层数据的边界

| 层 | 定义 | 可变性 | 存放位置 |
| --- | --- | --- | --- |
| Raw Evidence | Benchmark 原始文件，字节不可修改 | 不可变 | CAS（内容寻址，sha256 digest） |
| Normalized Evidence | Adapter 产出的统一文档和 observations | 不可变（重算会产生新 digest） | CAS + Evidence Manifest 索引 |
| Derived Metrics | 从 Raw/Normalized Evidence 后算的指标 | 追加式，允许多版本共存 | Derived Metric Ledger / Analysis Snapshot |

判定规则：

- 原始 Trace 固化。修改分析算法或目标函数时，只重算 Derived Metrics，不重新执行昂贵的 Benchmark。
- 每一层都能向上追溯：Normalized → Raw artifact digest + Adapter ID/版本/实现 digest；Derived → 输入 artifact digests + traceSetDigest + 工具 ID/版本/digest + parameters digest。
- 缺失必需证据、单位不一致、Artifact 损坏、Trace 缺 rank 时 fail closed：要么抛错，要么把 Evidence 标记为 `failed` 并记录失败的 check，绝不以 0 或空值代替。

## 3. 核心对象

所有模型定义在 `packages/core/looper_core/evidence.py`，JSON Schema 在 `schemas/` 下（`evidence-manifest`、`environment-snapshot`、`trace-set`、`derived-metric`），Pydantic 模型与 JSON Schema 双重校验且都拒绝未知字段。

### Evidence Manifest

一次 Attempt 的完整证据清单（`EvidenceManifest`），包含：

- 身份：`schemaVersion`、`experimentId`/`candidateId`/`evaluationId`/`attemptId`、`benchmarkId` + `benchmarkVersion` + `benchmarkManifestDigest`、`workloadId`、`candidateConfigDigest`；
- 环境：`environmentDigest`（可内嵌 `EnvironmentSnapshot`）；
- 三类工件清单：`rawArtifacts`、`normalizedArtifacts`、`traceSets`；
- 数据：`normalizedObservations`、`derivedMetrics`（追加区）、`result`（含 checks）；
- 来源：`adapter`（AdapterIdentity）、`createdAt`。

每个 Artifact 条目（`EvidenceArtifact`）至少包含 `digest`、`size`、`role`、`mediaType`、`producer`、`name`、`required`、`provenance`。

`evidence_content_digest` 计算 manifest 的规范摘要（排除 `evidenceId`、`createdAt`、`derivedMetrics` 这三个易变字段），因此同一输入必然得到同一 `evidenceId`，而事后追加 Derived Metric 不会改变证据身份。

### Environment Snapshot

`EnvironmentSnapshot` 是系统指纹之上的稳定环境契约，可容纳 CPU/NUMA/内存/磁盘/NIC、加速器（型号/数量/UUID/PCIe/NVLink）、互联拓扑、驱动、CUDA/ROCm、通信库（NCCL/RCCL 等）、框架、编译器、容器镜像 digest 和影响性能的环境变量。

无法采集的字段一律 `null`，绝不从 CPU 侧推测 GPU 侧。合成环境必须置 `synthetic: true`（见 `synthetic_gpu_environment()`），真实机器数据由后续 collector 提供。

### Trace Set Manifest

Trace 不作为普通 Artifact，而是一组有逻辑关系的索引（`TraceSetManifest`）：格式（pytorch-kineto / xprof / nsys / looper-synthetic-json）、时间单位与时钟域、collector 名称/版本/配置 digest、warmup/measurement 步数区间、step boundary 规则、rank→artifact 映射、`complete` 与 `missingRanks`（二者必须互相印证，模型校验强制）。原始大文件留在 CAS，manifest 只存 digest 与元数据。

### Derived Metric

`DerivedMetric` 单独记录每个后算指标：`metric`、`value`、`unit`、`statistic`、`analysisToolId/Version/Digest`、`analysisInputDigest`、`inputArtifactDigests` 或 `traceSetDigest`、`parameters` + `parametersDigest`、`generatedAt`。同一原始证据上，不同工具版本或不同参数的结果并存，旧结果永不覆盖（`DerivedMetricLedger` 是 append-only）。

## 4. 如何为新 Benchmark 编写 Adapter

Adapter 契约在 `packages/core/looper_core/evidence_adapters.py`。概念上：

```
normalize(native_input, run_context) -> normalized_evidence
```

步骤：

1. 继承 `UnifiedAdapter`，声明类属性：`adapter_id`、`adapter_version`、`source_format`、`upstream_id`、`manifest_path`（用于实现 digest）、`metric_units`（指标目录，用于单位一致性检查）、`synthetic`、`compatibility_status`、`upstream_license`。
2. 实现 `_raw_inputs(source)`：声明要包装的原始文件（`RawInput`）。默认缺失即抛错（fail closed）；trace 类文件可用 `missing_policy="record"`，让缺失体现在 Evidence 的 `trace-set-completeness` check 里而不是直接崩溃。
3. 实现 `_normalize(source, context)`：解析上游格式、做字段映射与单位归一化、产出 `AdapterNormalization`（normalized 文档 + `MetricObservation` 列表 + checks + 可选 `TraceSetManifest`）。
4. 框架负责其余一切：逐文件 digest、构造 `EvidenceArtifact`、单位目录校验、组装 `EvidenceManifest`、计算 `evidenceId`。

约束：Adapter 不修改原始文件、不做优化决策、不感知下游。上游特定解析只允许出现在 Adapter 层——下游只有 `summarize_evidence()` 这一个与 Benchmark 无关的读取接口。

现有三个实现可作参考：

- `CclWorkloadCardAdapter`（CCL-style Workload Card + Trace）；
- `BenchbaseSmallbankEvidenceAdapter`（复用 `load_benchbase_smallbank_fixture`）；
- `DcperfMediawikiEvidenceAdapter`（复用 `load_dcperf_mediawiki_fixture`）。

三者输入格式完全不同，但产出同一种 `EvidenceManifest`，下游用同一接口读取（见 `tests/test_unified_adapters.py::test_downstream_reader_never_branches_on_benchmark`）。

## 5. 数据流与目录结构

```
adapters/<benchmark>/            # 每个基准：manifest、README、合成 fixture
  adapter.manifest.json          # 实现 digest 的锚点
  fixture/                       # 合成原始输出（workload card、trace、summary 等）
packages/core/looper_core/
  evidence.py                    # 统一契约（Pydantic 模型 + digest + 校验入口）
  evidence_adapters.py           # UnifiedAdapter 框架 + 三个具体 Adapter
  trace_evaluator.py             # 离线 Trace 评估器 + DerivedMetricLedger
schemas/
  evidence-manifest.schema.json  # 与 Pydantic 模型对应的 JSON Schema
  environment-snapshot.schema.json
  trace-set.schema.json
  derived-metric.schema.json
tests/
  test_evidence_contract.py      # 契约与 schema 校验测试
  test_unified_adapters.py       # 三源归一、fail-closed、provenance 测试
  test_trace_evaluator.py        # 离线重算与多版本共存测试
```

## 6. CCL-compatible Adapter 的边界

`CclWorkloadCardAdapter` 是原创的兼容层，只借鉴 CCL-Bench 的 Evidence → Analysis 思路：

- 输入是本仓库自制的合成 fixture（`adapters/ccl-workload-card/fixture/`），不包含、不复制任何上游源码、文档或真实 Trace；
- Adapter 身份中显式标注 `synthetic: true`、`sourceFormat: workload-card-yaml`、`compatibilityStatus: compatible`、`upstreamLicense: unresolved`；
- 上游许可证状态在 Looper 中尚未确认。在通过 `third_party/sources.lock.yaml` 的许可证检查之前，不得 fetch、内嵌或重新发布上游内容；
- 该 Adapter 不代表 CCL-Bench 的真实行为，只验证"这类格式的输出可以进入统一底座"。

## 7. 如何离线重新计算指标

Trace Evaluator（`packages/core/looper_core/trace_evaluator.py`）演示最小闭环：

```python
from looper_core.evidence_adapters import CclWorkloadCardAdapter, RunContext, synthetic_gpu_environment
from looper_core.trace_evaluator import (
    TraceEvaluationParameters, DerivedMetricLedger,
    evaluate_trace_set, trace_evaluator_tool,
)

# 1. 标准化一次（读取合成 workload card + trace fixture）
bundle = CclWorkloadCardAdapter().normalize(
    Path("adapters/ccl-workload-card/fixture"),
    RunContext(benchmarkId="ccl-bench-compatible",
               environment=synthetic_gpu_environment()),
)
trace_set = bundle.manifest.trace_sets[0]

# 2. 从 CAS 取回原始 trace 字节（这里直接读 fixture 文件）
payloads = {name: (fixture_dir / name).read_bytes()
            for name in ("trace-rank0.json", "trace-rank1.json")}

# 3. 用工具版本 1.0.0 计算 average step time 与通信占比
ledger = DerivedMetricLedger()
ledger.extend(evaluate_trace_set(
    trace_set, payloads,
    tool=trace_evaluator_tool("1.0.0"),
    parameters=TraceEvaluationParameters(),
))

# 4. 换参数（含 warmup）重新计算——不重新运行 Benchmark
ledger.extend(evaluate_trace_set(
    trace_set, payloads,
    tool=trace_evaluator_tool("1.0.0"),
    parameters=TraceEvaluationParameters(include_warmup=True),
))

# 5. 两个版本的结果并存，旧结果不会被覆盖
assert len(ledger.records()) == 4
```

要点：

- Evaluator 只读取 `TraceSetManifest` 和它引用的原始 trace 字节，不接触任何 Benchmark；
- `analysis_input_digest` 绑定 traceSetDigest、所有 artifact digest、工具 ID/版本/digest 和参数，因此换工具版本或换参数都会生成新的输入摘要；
- 同一 trace 的原始 digest 在两次计算之间完全不变（测试断言这一点）；
- 不完整的 trace set（缺 rank）或 CAS 缺文件时直接抛 `TraceEvaluationError`；
- 换新工具版本（如 `trace_evaluator_tool("2.0.0")`）再算一遍，Ledger 中两个版本共存。

## 8. 数据库与存储的选择

本阶段没有新增任何数据库表。理由：

- 原始与标准化工件全部走现有 CAS（内容寻址文件存储），数据库不保存大文件；
- Evidence Manifest 及其 digest、环境快照、trace set 元数据都是 JSON 文档，可以直接挂到现有 `ArtifactRecord` / `ArtifactLinkRecord` / `AttemptRecord` / `ObservationRecord` / `AnalysisSnapshotRecord` 的 JSON 载荷与关联结构上；
- Derived Metrics 的"多版本共存、不覆盖"语义由 `DerivedMetricLedger` 与 Analysis Snapshot 的 input digest（包含 artifact、trace set、工具版本、参数）承担，不需要新唯一约束。

因此不需要新的 Alembic migration，旧数据库无升级风险。若后续 Derived Metric 数量增长到需要按 (metric, tool_version, parameters_digest) 索引查询，再评估专门的表和 migration。

## 9. Schema 版本升级规则

- 当前所有契约为 `v1alpha1`：字段只增不删；收紧校验前必须先评估已有数据的兼容性。
- Pydantic 模型（`StrictModel`，拒绝未知字段）与 `schemas/*.schema.json`（`additionalProperties: false`）必须同步修改，任何一方的收紧都意味着契约版本变化。
- 升级到 `v2` 时新增 schemaVersion 字面量并提供迁移函数；`v1alpha1` 文档应能被读取，但写出一律用新版本。
- `evidence_content_digest` 的计算字段集合（排除哪些易变字段）属于契约的一部分，变更即视为版本升级。
- JSON Schema 位于 `https://looper.dev/schemas/v1alpha1/...` 命名空间下，$id 随版本变化。

## 10. 当前仍未实现的部分

- 真实 GPU 环境采集：`EnvironmentSnapshot` 的 GPU/互联/驱动字段已有契约与合成 fixture，但没有 collector 实现，字段保持 `null`。
- 真实 Trace 格式：Evaluator 只实现 `looper-synthetic-json`；pytorch-kineto、xprof、nsys 解析器未实现。
- CCL-Bench 真实集成：许可证未确认，只有兼容层和合成 fixture。
- Evidence Manifest 与数据库记录的正式挂载：当前 Adapter 在进程内产出 Evidence 并可写入磁盘（`write_evidence_bundle`），与 `AttemptRecord`/`ArtifactRecord` 的自动关联和 API 查询接口留给下一阶段。
- Derived Metric Ledger 目前是内存 append-only 结构，尚未持久化到数据库或 CAS。
- 多 Trace Set 联合分析与更丰富的统计（分布、尾延迟）属于后续分析阶段。
