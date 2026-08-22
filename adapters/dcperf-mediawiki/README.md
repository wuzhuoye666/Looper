# DCPerf MediaWiki Closed-Loop Scenario Adapter

This Stage 0 adapter normalizes `oss_performance_mediawiki_mlp` output from
DCPerf revision `9308c3e3c404e0466f0a2929f15ddcf62b2215f6`.

The primary metric is successful requests divided by Wrk wall time. Upstream
`Wrk RPS` is retained only as a diagnostic because it includes failed requests.
The adapter also requires Looper's sidecar monitor to show at least 90% p95 CPU
utilization. Because Wrk, HHVM, nginx, MediaWiki, memcached, and MariaDB share
the target, this is explicitly a single-VM closed-loop full-stack result, not a
pure server or CPU capacity result.

The normalization-only executable converts a pinned Benchpress result into the
Looper worker contract without installing or starting the MediaWiki stack:

```text
looper-dcperf-mediawiki-normalize \
  --result <benchpress-result.json> \
  --output <looper-output>
```

It emits `metrics.jsonl`, `result.json`, `normalized-result.json`, preserves the
upstream result, and records the pinned source revision. Tail aggregates carry
the successful-request count as their underlying sample evidence. This command
is the container integration boundary, not a workload runner.

The fixture is synthetic and follows the reporting example in the pinned
upstream README. It is parser evidence only. Pass `--synthetic-fixture` when
using it so that every emitted observation is tagged accordingly.
