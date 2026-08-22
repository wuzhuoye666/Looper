# Experiment and Analysis Contract

## Modes

`ExperimentSpec.mode` is explicit:

- `selection` is the primary server-procurement flow. It requires `scenario` and `selection`, forbids optimizer search parameters, and binds every `target_id` exactly once through a `TargetBindingSpec`.
- `optimization` preserves candidate search for the trusted compression demo. It requires a complete search space, baseline parameters, and no scenario/selection objects.

The default is `optimization` for backward compatibility with stored specs. New server studies must set `mode: selection`.

## Scenario identity

A selection study references an installed benchmark ID/version and embeds the parsed scenario contract. The embedded contract must equal the installed manifest's contract after canonical model normalization. A scenario includes:

- procurement decision question and user value;
- workload class, topology, and explicit roles;
- primary metric;
- committed-goodput policy;
- SLO gates;
- optional tail-evidence and load-search contracts.

Changing the manifest, scenario, target binding, placement assignment, price snapshot, or evidence design changes the experiment spec digest.

## Target and placement identity

Each `TargetBindingSpec` contains:

- `target_id`: immutable target snapshot owner;
- `variant_id`: SKU or server variant being compared;
- `label`: presentation label;
- `placement_pair_id`: independent placement cluster identifier;
- optional `PriceSnapshot`: hourly amount, currency, quote digest, effective time, and provider.

Multiple target IDs may share a variant ID when reproducing one SKU across placements. Repeats on one target do not create new placements.

## Candidate, evaluation, and attempt identity

In optimization mode, a Candidate is an immutable canonical parameter object bound to a benchmark manifest digest. Retrying never edits a candidate or attempt; it appends an attempt with a larger retry index.

In selection mode, one internal Candidate with role `scenario` owns the matrix without implying optimization. An Evaluation is `(scenario candidate, workload, target snapshot)`. An Attempt is one repeat or retry. Its envelope includes `timeBlockId`, target binding, scenario, selection design, and target/system snapshots.

Warmup observations are labeled and excluded from measurement analysis. Retries remain evidence but only the successful terminal attempt contributes to a valid block.

## Scheduling contract

For each workload and repeat, selection scheduling groups targets by placement pair. Initial placement and target order are seeded, then rotated each repeat. This creates adjacent paired blocks and balances first/second order. The order seed is part of the immutable selection design.

A scheduler must not flatten `(placement pair x repeat)` into independent observations. Time blocks are the inference unit for one placement. Placement pairs are the inference unit for cross-placement claims.

## Draft and execution readiness

Selection studies can be created as `draft` against adapter-only benchmarks and inventory-only targets. Starting or resuming is fail closed when any execution prerequisite is absent:

- installed benchmark version;
- `executionStatus: executable`;
- digest-pinned container image for container runtimes;
- runnable targets;
- target capabilities covering manifest capabilities.

`stage0-adapter-only` means normalization contracts and fixtures exist, not that the workload can run.

## Status invariants

Experiment transitions:

- `draft -> queued -> running`
- `queued|running -> paused -> queued`
- `draft|queued|running|paused -> cancelled`
- `running -> completed|failed`

Attempt transitions:

- `queued -> leased -> running -> uploading -> succeeded`
- active attempts may end as `failed`, `timed_out`, `cancelled`, or `lost`
- terminal attempts never return to an active state

Only queued attempts can be leased. Every lease increments a fencing token. Heartbeat, artifact and completion commits require the current token.

## Observation and artifact integrity

Every observation must use a metric and unit declared in the pinned manifest. Required metrics must be present and satisfy `minimumSamples` using either individual observation count or an aggregate's explicit `sampleCount`. Required artifacts are checked independently by exact path.

For scenario metrics:

- goodput includes only committed or successful work;
- abort, deadlock, rollback, retry, timeout, and error counts remain separate;
- p50/p95/p99/p99.9/max aggregates carry underlying sample count;
- raw latency or a lossless histogram is retained when required by the scenario.

Missing, non-finite, undersampled, or unit-mismatched evidence fails the Attempt; it never becomes a zero score.

## Gate ordering

1. Execution integrity: process status, required metrics, units, sample evidence, artifacts.
2. Correctness and safety checks.
3. Per-block SLO, environment, and resource gates.
4. Target/frontier classification.
5. Statistical evidence and minimum effect.
6. Decision rendering with explicit conclusion strength.

Optimization mode adds Pareto ranking after feasibility. Selection mode does not rank invalid target evidence.

## SLO frontier contract

A load point is classified from repeated `FrontierBlockEvidence`. A block passes only when correctness, environment consistency, error/abort/timeout gates, latency sample count, latency SLO, at least 99% offered-load achievement, less than 1% rate-limiter lag, and at least 20% client headroom all pass. `latency_samples` names the population carried by the p99 aggregate; it is not mislabeled as committed samples when upstream latency covers all measured attempts. The load point is confirmed pass only at the configured required pass count; failures, missing blocks, or contradictory evidence are explicit.

`analyze_slo_frontier` reports:

- highest confirmed passing load;
- lowest confirmed failing load;
- interval bounds and relative width;
- unresolved points;
- non-monotonic evidence;
- next load under bracket/binary-search policy.

The result is an interval, not the maximum of a finite load grid. A terminal ceiling with no failing bracket remains right-censored.

## Selection inference

For exactly two variants in one placement, paired bootstrap resamples common time-block indices together. For multiple placements, Looper aggregates each variant inside every common placement pair, then resamples placement-pair indices together. The independent-sample `bootstrap_improvement` API is not valid for paired claims.

An improvement is distinguishable only when its confidence interval excludes zero and the point estimate meets the scenario minimum-effect ratio. Result labels are evidence levels, not hardware-family claims:

- one target: availability only;
- one placement pair: provisional;
- two to four placement pairs: exploratory;
- at least five placement pairs: procurement-candidate evidence.

Price-normalized capacity is computed only from an immutable target price snapshot and links back to its quote digest.

## Analysis snapshot identity

Every analysis snapshot binds an input digest, policy digest, and code version. The policy includes mode, objective direction/unit/aggregation/comparison, scenario contract, target bindings, placement design, gates, repeat policy, tail threshold, confidence, resample count, random seed, and code version. Reanalysis with changed facts or policy creates a new snapshot.

Optimization snapshots retain feasibility and Pareto policy. Selection snapshots retain block evidence, target results, paired comparisons, invalid-block counts, inference unit, placement-pair count, confidence interval, minimum effect, winner when distinguishable, and no-conclusion reason otherwise.
