# Looper Architecture

## Product boundary

Looper is a scenario-based server benchmark suite for procurement and instance selection. Its primary unit is a `selection` study: an immutable procurement question, installed scenario contract, target bindings, fairness design, evidence requirements, and budget. The legacy `optimization` mode remains for the trusted local compression demo and continues to use candidate search and Pareto analysis.

A scenario contract declares workload topology and roles, goodput accounting, SLO gates, tail evidence, load-search semantics, and the primary decision metric. A target binding declares target, SKU variant, placement pair, label, and optional immutable price snapshot. Runtime readiness is separate from catalog availability.

## Control plane

The FastAPI process is the only metadata writer in local mode. It validates both experiment modes, appends lifecycle events, creates evaluations and attempts, grants fenced worker leases, commits content-addressed artifacts, creates immutable analysis snapshots, and coordinates cloud quote/order state. SQLite WAL is supported only on a local filesystem with one API process.

Selection studies may be saved while a benchmark or target is not executable. Start is fail closed unless all of the following hold:

- the benchmark manifest reports `executionStatus: executable`;
- a container runtime uses an image pinned with `@sha256:`;
- every target is runnable and exposes all benchmark capabilities;
- the installed scenario in the experiment matches the installed manifest exactly.

The Stage 0 BenchBase and DCPerf workloads intentionally fail this start gate. Their installed normalization-only commands exercise the upstream-output-to-worker contract locally, but no upstream launcher, digest-pinned image, or matching container/remote executor exists yet.

## Scheduling and pairing

Optimization mode creates baseline and candidate evaluations using the existing replayable optimizer.

Selection mode creates one scenario candidate as an internal ownership record; it does not launch an optimizer loop. Evaluation identity is `(workload, target)`. Attempts are ordered by workload and repeat block. Within each placement pair, target order is seeded once and rotated on each repeat, so paired targets remain adjacent while first/second position is balanced. The run envelope carries the scenario contract, selection design, target binding, repeat index, and stable `timeBlockId`.

Order balancing does not manufacture independence. Repeats inside one placement are time-block pairs. With multiple placements, analysis aggregates repeats inside each placement pair and then resamples placement pairs as clusters.

## Analysis boundary

Attempts, observations, checks, artifact links, environment snapshots, and events are immutable facts. Experiment, candidate, evaluation, and attempt statuses are mutable query projections. Analysis snapshots are keyed by:

- canonical input-fact digest;
- analysis-policy digest, including mode, objectives, scenario, selection design, gates, and repeat policy;
- analysis code version.

Optimization snapshots retain feasibility, confidence, Pareto ranks, rank stability, and sensitivity.

Selection snapshots group evidence by target, variant, time block, workload, and placement pair. They report valid and invalid block counts, target metrics, optional price-normalized capacity, and one of these conclusion strengths:

- `availability-only`: no two-variant comparison;
- `single-placement-provisional`: paired time-block evidence from one placement;
- `multi-placement-exploratory`: two to four placement pairs;
- `procurement-candidate`: at least five placement pairs under the same design.

Single-placement comparison uses paired bootstrap over common time blocks. Multi-placement comparison first aggregates within placement and then uses cluster-paired bootstrap. Independent-sample bootstrap is never used for a paired claim.

The SLO frontier core classifies each load from repeated blocks, requires the configured pass count, detects non-monotonic evidence, and returns `[highest confirmed pass, lowest confirmed fail]`. A finite tested grid is not relabeled as a frontier.

## Goodput and tail evidence

Scenario adapters normalize upstream output without redefining workload semantics. Goodput excludes aborts, deadlocks, rollbacks, retries, timeouts, and errors. BenchBase summary throughput is observed attempted throughput, while planned offered TPS comes from reconciled client-load accounting. DCPerf successful RPS is derived from successful requests divided by measured wall time; upstream `Wrk RPS` is retained only as diagnostic evidence.

Required metric declarations include `minimumSamples`. Attempt completion verifies actual observation count or the aggregate observation's underlying `sampleCount`. A required tail aggregate with inadequate evidence fails the attempt. Required histogram/raw artifacts are enforced independently through manifest outputs.

## Worker boundary

Workers register capabilities and claim attempts over HTTP. Every lease has a monotonically increasing fencing token. Every heartbeat, artifact upload, and completion carries that token; stale workers cannot overwrite a retried attempt. Benchmark commands receive a minimal environment and isolated input/output/work directories.

The shared `looper.system-fingerprint/v1alpha1` collector records CPU identity, flags, microcode, topology, cache and NUMA, SMT, governor/EPP, THP, tuning daemons, cgroups, boot command line, swap, NICs, disks, and runtime versions. Fields unavailable to the guest are `null`; they are not inferred.

## Cloud provider boundary

The cloud service depends on a typed Provider contract rather than SDK response shapes. Provider adapters normalize regions, zones, instance types, images, quotes, and creation results from four official SDKs. Catalog data is TTL-cached with explicit stale fallback. Quote snapshots bind a canonical launch-spec digest.

Real purchase is independent from selection-study creation and is guarded by operator authentication, a global switch, provider allowlist/readiness, HMAC confirmation, quote TTL, manual phrase entry, amount echo, and spend cap. Explicit failures require a new quote; ambiguous results enter `unknown` and are never retried automatically. Credentials remain process-environment inputs and are not persisted or inherited by benchmark commands.

## Storage and supported topology

Artifacts are streamed into a bounded same-volume temporary file, hashed, flushed, atomically renamed into a SHA-256 CAS, and verified on read. Metadata and blobs are separated behind replaceable interfaces.

The local topology supports one control-plane API process, one local filesystem, and a bounded set of workers. It does not support SQLite on NFS, multiple API writers, remote exposure without additional authentication, or untrusted local-process benchmarks. PostgreSQL, COS, remote workers, and a container/BenchExec executor are explicit extension points.
