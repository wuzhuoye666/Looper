# BenchBase SmallBank Scenario Adapter

This Stage 0 adapter normalizes the pinned BenchBase revision
`33c00473807ebd49304d114a6d769d2d2b2bbb34` for the PostgreSQL SmallBank
selection scenario.

It reads the upstream `.summary.json`, `--json-histograms` outcome counters,
and `.raw.csv` transaction latencies. `committed_tps` is derived from completed
transactions and elapsed time. Aborts, server retries, unexpected errors, and
timeouts never enter the goodput numerator.

The normalization-only executable converts upstream files into Looper's worker
contract without starting BenchBase:

```text
looper-benchbase-smallbank-normalize \
  --summary <benchbase.summary.json> \
  --histograms <benchbase.histograms.json> \
  --latencies <benchbase.raw.csv> \
  --client-accounting <client-load-accounting.json> \
  --output <looper-output>
```

It emits `metrics.jsonl`, `result.json`, `normalized-result.json`, preserves the
raw latency and outcome histogram files, and records the pinned source revision.
The accounting sidecar must bind the pinned `<work><rate>` to planned, offered,
started, completed and timeout counts for the exact measurement window, plus
rate-limiter lag and isolated-client headroom. BenchBase's summary throughput is
actual attempted throughput, not planned load. The normalizer rejects mismatched
windows or counts. This command is the container integration boundary, not a
workload runner.

The fixture is synthetic but follows the field names emitted by the pinned
`ResultWriter` and `Histogram` implementations. It is parser evidence only and
must not be reported as a benchmark result. Pass `--synthetic-fixture` when
using it so that every emitted observation is tagged accordingly.
