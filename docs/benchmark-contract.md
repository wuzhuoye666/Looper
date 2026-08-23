# Benchmark Contract v1alpha1

Looper benchmark plugins are data contracts plus structured commands. The coordinator never imports benchmark code into the control-plane process. A procurement benchmark may additionally declare `spec.scenario`; this changes decision semantics, not process isolation.

## Scenario contract

`spec.scenario` defines a real workload and procurement question. It is not a tuning profile. The contract fixes workload class and topology, scored and supporting roles, committed-goodput accounting, SLO gates, tail evidence, primary metric, and optional load-search policy.

Adapters must preserve upstream workload semantics. They may normalize names and derive committed/successful rates from upstream counters, but they may not count retry, rollback, abort, timeout, or error work as goodput. Diagnostic upstream rates remain explicitly diagnostic.

For an open-loop load search, planned load is an input fact, not a synonym for observed throughput. `client-load-accounting.schema.json` binds planned rate, measurement duration, offered, started, completed and timeout counts, rate-limiter lag, and client headroom. The chain must close (`completed + timeout = started <= offered`) and the runtime normalizer must reconcile its window and completed count with upstream output. BenchBase summary `Throughput (requests/second)` is normalized as `attempted_tps`; only the pinned work rate becomes `offered_tps`.

`x-extensions.executionStatus` discloses runtime maturity. `stage0-adapter-only` means the manifest, parser and fixtures validate locally but no digest-pinned execution image/runner exists. It must never be treated as runnable.

## Lifecycle

`prepare -> warmup -> run -> validate -> collect -> cleanup`

`run` is required. Other phases are optional, but `cleanup` runs after success, failure, timeout, or cancellation when it is declared. Commands are argv arrays, not shell strings. The worker expands only the documented placeholders:

- `{python}`: current worker Python executable
- `{cache}`: persistent, Benchmark-version-scoped dependency cache on the Worker
- `{input}`: isolated input directory
- `{output}`: isolated output directory
- `{workspace}`: per-attempt workspace
- `{envelope}`: path to `run-envelope.json`

Unknown placeholders fail validation. Plugins cannot reference paths outside their allocated roots.

## Outputs

The measurement phase writes one JSON object per line to `metrics.jsonl`. Every line validates against `schemas/metric-observation.schema.json`; non-finite numbers, unit mismatches, duplicate sample identities, oversized lines, and excessive line counts invalidate the attempt.

The plugin writes `result.json` using `schemas/result.schema.json`. A runtime scenario adapter must translate pinned upstream output into these standard files while retaining the declared upstream-shaped raw/histogram artifacts. Aggregate tail observations carry the underlying `sampleCount`; satisfying a metric name without its declared `minimumSamples` fails completion.

Stage 0 provides installed normalization-only commands for BenchBase SmallBank and DCPerf MediaWiki. They produce `metrics.jsonl`, `result.json`, `normalized-result.json`, logs, and preserved source artifacts; malformed or unreconciled input writes failed result evidence and exits non-zero. They do not start PostgreSQL, BenchBase, or the DCPerf stack and therefore do not change `executionStatus: stage0-adapter-only`.

Declared artifacts are resolved below the output root with path and symlink checks. Missing required artifacts fail the execution-integrity gate. The `histogram` role identifies latency distributions or lossless raw latency evidence.

## Trust

`trusted` permits the local-process runner after explicit installation approval. `untrusted` requires a container or BenchExec backend. Secrets used by the control plane are never inherited by benchmark commands.

For newly purchased or otherwise clean machines, declare `runtime.provisioning.mode: managed`.
`hostCapabilities` are the small set that must exist before deployment (for example
`linux`, `python`, and `local-process`); `provides` lists Benchmark-specific software
installed or materialized by `commands.prepare`. Looper matches candidate resources
against `hostCapabilities`, delivers the complete immutable Benchmark ZIP after the
user starts the experiment, runs `prepare`, and only then starts the workload. The
prepare command must be idempotent, verify every downloaded digest, and reuse
`{cache}`. Do not require users to preinstall items listed in `provides`.

## Metric presentation semantics

Measurement fields (`unit`, `direction`, `kind`, `required`, `minimumSamples`) describe how a metric is *produced and validated*. They do not tell a user-facing view what the metric means. Every metric may optionally declare a `presentation` object that supplies display semantics without polluting the raw measurement meaning:

```yaml
metrics:
  committed_tps:
    unit: transactions/second
    direction: maximize
    kind: aggregate
    required: true
    minimumSamples: 1
    presentation:
      userLabel: 提交吞吐量
      userDescription: 每秒成功提交的事务数，是用户关心的主结果。
      roles: [primary_outcome]
      defaultVisibility: summary
      displayFormat: throughput
      displayPrecision: 2
```

`presentation` is fully optional. A benchmark without it still loads and executes; consumers fall back to the metric name and measurement fields. All `presentation` sub-fields are optional too.

### Roles

`roles` is an array of controlled vocabulary values. A single metric may carry several roles because it can serve several purposes at once (for example a p99 latency that doubles as a hard gate and a guardrail). Duplicate entries are rejected.

| Role | Meaning |
| --- | --- |
| `primary_outcome` | The result a user optimizes toward; the headline number. |
| `hard_gate` | A MUST-PASS condition gate. Failure disqualifies the result. |
| `guardrail` | A protected metric that must not regress meaningfully while the primary outcome improves. |
| `cost_efficiency` | A unit-cost or throughput-per-cost measure. |
| `stability` | Dispersion or invariance across repeats / environment axes. |
| `diagnostic` | Helps locate a problem; does not represent user benefit. |
| `context` | Describes only the environment or test condition, not an outcome. |

Diagnostic and context metrics must never be presented as user benefit.

### Visibility

| Value | Where it belongs |
| --- | --- |
| `summary` | Headline summary; usually `primary_outcome`. |
| `detail` | A secondary result panel; guardrails and hard-gate evidence. |
| `expert` | Diagnostics and raw counters useful for debugging. |
| `hidden` | Correctness/boolean flags, infrastructure plumbing; shown only on demand. |

`displayFormat` is a rendering hint (`number`, `percent`, `duration`, `bytes`, `throughput`, `boolean`); `displayPrecision` is a non-negative decimal hint; `glossary` may hold a short term definition.

### Declaring metrics as a benchmark author

Add `presentation` under any metric in `spec.metrics`, or override display semantics per workload via `workload.metrics.<name>.presentation`. Workload-level declarations may carry only `presentation`; measurement fields stay on the spec-level metric unless the workload intentionally overrides them.

### Compatibility when presentation is absent

- Schema validation does not require `presentation`.
- `manifest.py` and `seed.py` are unchanged; a legacy benchmark loads exactly as before.
- Serialized API output exposes `metricDefinitions` in addition to the existing `metrics` string list. A metric without `presentation` simply has no `presentation` key in its definition, and clients fall back to the metric name.

### Why names and array order are not semantics

Metric names are adapter-controlled and can change between benchmark versions; array order is an implementation detail of the YAML mapping. Deriving "this is the primary metric" from either is fragile and forbidden. The authoritative primary metric is `scenario.primary_metric` (or `adapter.primaryMetric` for non-scenario benchmarks); `presentation.roles` then describes *why* a user cares about it.

### `hard_gate` role vs `scenario.slo_gates`

The `hard_gate` presentation role is a display hint only. It neither defines nor enforces a threshold. Actual gates are declared in `scenario.slo_gates` (or the formal validation contract), which carry the operator, threshold, and scope. A metric may be listed as a `hard_gate` role without a corresponding `slo_gates` entry, and vice versa; only the `slo_gates` entry is binding.

## Versioning

`looper.dev/v1alpha1` rejects unknown behavior-bearing fields. Vendor data belongs under `x-extensions` or result `extensions`. A future incompatible lifecycle or metric interpretation receives a new API version; changing only a benchmark parameter or workload increments the benchmark's own version and manifest digest.
