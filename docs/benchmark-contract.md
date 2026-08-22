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

## Versioning

`looper.dev/v1alpha1` rejects unknown behavior-bearing fields. Vendor data belongs under `x-extensions` or result `extensions`. A future incompatible lifecycle or metric interpretation receives a new API version; changing only a benchmark parameter or workload increments the benchmark's own version and manifest digest.
