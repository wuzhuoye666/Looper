# P0 Benchmark Evaluation Framework

Status: aligned for CPU pilot design on 2026-08-21
Evidence cutoff: 2026-08-21
Audience: Looper P0 research and review

## 1. Product scope

Looper is a scenario-oriented benchmark suite for server procurement and instance selection.
It should answer a user question such as:

> Under one controlled workload and SLO, which server or cloud instance delivers the best
> sustainable performance, stability, and cost, and what hardware or platform facts explain
> the difference?

A scenario is therefore a concrete workload plus a decision objective. Examples include
Web/API service under a p99 latency SLO, database OLTP, LLM inference, agent runtime
concurrency, and multi-GPU collective communication.

A scenario is not an OS power profile, a per-application tuning recipe, or a generic score.
Tuning may be introduced later only as a declared, uniformly applied experimental factor.
Per-target bespoke tuning is not a fair default comparison.

## 2. P0 acceptance dimensions

Every candidate paper and benchmark is assessed on five core dimensions. Scores are evidence
levels, not subjective quality ratings.

| Score | Availability | User value | Completeness / gap fill | Fairness | Discrimination |
| --- | --- | --- | --- | --- | --- |
| 0 | No runnable artifact or inaccessible data | No server-selection question | Exact duplicate with no added evidence | Conditions are uncontrolled | No comparative result |
| 1 | Artifact named but cannot be built or data is unavailable | Synthetic mechanism with no workload mapping | Minor duplicate variation | Critical settings are undocumented | One absolute score only |
| 2 | Partially runnable; manual repair or unavailable dependency remains | Generic hardware proxy only | Adds one metric but not a material scenario | Some settings fixed, but placement/repeats are missing | Differences shown without uncertainty or attribution |
| 3 | Reproducible instructions and a runnable upstream path exist | Maps to a real workload and decision metric | Adds a material workload or hardware dimension | Versioned configuration, warm-up, repeats, and target facts are recorded | Multiple systems differ consistently with basic statistics |
| 4 | Automated, pinned, correctness-checked run with redistributable inputs | Production or user evidence supports the workload/SLO | Fills a documented high-priority suite gap | Cross-target protocol controls software, load, placement, and run order | Statistically supported differences plus plausible hardware attribution |
| 5 | Reproduced by Looper with raw evidence and recovery instructions | Directly supports a purchase/capacity/cost decision | Fills a priority gap while complementing the suite without single-task dominance | Multi-environment validation covers black-box variability and interference | Multi-generation/provider validation includes sensitivity or causal evidence |

Robustness is reported separately on a 0-5 scale. It covers architecture, accelerator,
provider, kernel, and deployment portability. It does not compensate for failure in a core
dimension.

| Score | Robustness evidence |
| --- | --- |
| 0 | The artifact is not runnable, so portability cannot be assessed. |
| 1 | One old or hard-wired platform is documented. |
| 2 | One primary platform works and partial ports exist, but important workloads differ or fail. |
| 3 | At least two target classes are documented with unresolved compatibility or parity gaps. |
| 4 | Multi-architecture, accelerator, provider, or deployment support is actively validated upstream. |
| 5 | Looper has reproduced equivalent workload semantics across the target classes used for selection. |

## 3. Decision rules

The five dimensions remain visible as a vector. Looper must not hide a weak dimension behind
one weighted total.

A candidate is `implementation-ready` only when:

- Availability is at least 3.
- User value is at least 3.
- Fairness is at least 3.
- Completeness or discrimination is at least 3.
- Source, dataset, model, container, and redistribution terms are known for the intended use.

A candidate may remain `research-priority` when its user value or gap-fill score is high but
its artifact, license, or reproducibility evidence is incomplete. It must not be presented as
ready for integration.

For a provisional Top 10 sort, use the unweighted sum only after the gates above. Final order
is reviewed using the full vector, source risk, run cost, and coverage balance. Equal weighting
is an initial screening convention, not a claim that all user populations value workloads
identically.

## 4. Required evidence record

Each candidate record must contain:

- Stable benchmark identity, upstream URL, paper DOI/arXiv/venue, and evidence cutoff.
- Exact source revision or release; branch names alone are not revisions.
- License evidence for code plus separate terms for datasets, models, images, and result data.
- Intended user, deployment scenario, purchase question, and primary SLO or metric.
- Workload topology: process, container, VM, node, cluster, client, server, and storage roles.
- Hardware dimensions exercised and known blind spots.
- Build, prepare, warm-up, run, validate, and cleanup steps.
- Required secrets, proprietary inputs, production traces, or restricted services.
- Published comparison evidence and the limits of its attribution.
- Local reproduction status: not attempted, blocked, smoke-tested, or fully reproduced.

Unknown values are written as `unknown`; they are never inferred from a repository name,
release badge, or paper abstract.

## 5. Fair-comparison protocol

### 5.1 Target control

Record and, where possible, hold constant:

- Provider, region, availability zone, tenancy model, instance family, and advertised limits.
- Bare-metal, dedicated-host, or virtual-machine topology; do not mix them silently.
- Image digest, OS, kernel, microcode, firmware, drivers, compiler, runtime, and dependency lock.
- vCPU topology, SMT, NUMA, memory capacity, storage class, network limits, and accelerator
  topology.
- Effective CPU governor/EPP, TuneD/PPD/TLP state, cgroup limits, power caps, and other hidden
  controllers.
- Dataset, model, benchmark revision, application configuration, request distribution, and
  random seeds.

### 5.2 Run design

- Separate preparation, warm-up, measurement, correctness validation, and cleanup.
- Use the same offered load or the same SLO-search procedure on all targets.
- Randomize or block run order to reduce time-of-day and host-placement bias.
- Repeat enough times to estimate uncertainty; three repeats are a smoke-test floor, not a
  universal statistical guarantee.
- Preserve every raw sample and failed attempt. Never replace missing or invalid data with zero.
- For tail latency, require a declared minimum sample count and report p50, p95, p99, p99.9,
  maximum, timeout rate, and coordinated-omission handling.
- For black-box cloud VMs, repeat across instance recreations or placement blocks when budget
  permits. A single VM cannot establish host variability.

### 5.3 Isolation and interference

- State whether client and server share a host, VM, NUMA node, or physical network path.
- Record noisy-neighbor controls and concurrent workload activity.
- Multi-node tests must identify same-host multi-VM, cross-host same-AZ, cross-AZ, and
  cross-region cases separately.
- A local client/server smoke test does not validate physical NIC, virtual network, or fabric
  behavior.

## 6. Discrimination protocol

A benchmark demonstrates useful discrimination only when it:

1. Runs on at least two relevant targets; a single target proves availability only.
2. Reports effect size and uncertainty, not just two point estimates.
3. Repeats the comparison across placements or time blocks when the environment is black-box.
4. Shows that rank is stable enough for a purchase decision.
5. Connects the result to measured facts such as CPU generation, memory bandwidth, NUMA,
   storage latency, network PPS, accelerator topology, throttling, or virtualization steal.
6. Checks that one subtest or one weight does not dominate the suite without user evidence.

Existing Looper analysis helpers already support bootstrap intervals, coefficient of variation,
rank stability, environment sensitivity, and task leverage. P1 should expose these as selection
evidence rather than as optimizer diagnostics.

## 7. Coverage map

The suite is assessed against this initial coverage map:

| Coverage area | Representative decision question | Required metric classes |
| --- | --- | --- |
| CPU and ISA | Which instance completes compute work fastest per price? | completion time, rate, scaling, cost |
| Memory and NUMA | Which server sustains the workload without remote-memory penalties? | bandwidth, latency, remote access, scaling |
| Storage | Which storage/server combination meets throughput and tail-latency needs? | IOPS, bandwidth, p95/p99, durability errors |
| Network and RPC | Which instance sustains service load under a tail-latency SLO? | goodput, p99/p99.9, retransmit, CPU/request |
| Database/cache | Which server supports the target transaction or cache workload? | TPS/QPS, transaction latency, hit rate, recovery |
| Data analytics | Which server finishes representative analytics work at lowest cost? | job time, throughput, memory/storage pressure |
| AI inference | Which accelerator/server meets latency, accuracy, and throughput constraints? | latency, samples/tokens per second, accuracy, power |
| AI training | Which system reaches target quality fastest and scales efficiently? | time-to-quality, step time, scaling, power |
| Agent runtime | How many concurrent agents can meet end-to-end latency and reliability SLOs? | completed tasks/s, p99, success rate, resource/task |
| GPU supernode | Does the multi-GPU fabric feed the target distributed workload efficiently? | collective bandwidth, scaling efficiency, step variance |
| Serverless/workflows | Which platform handles burst, cold start, and workflow fan-out best? | cold/warm latency, throughput, cost, failure rate |
| Virtualization variability | How stable is delivered performance across placements and neighbors? | variance, steal, rank stability, interference sensitivity |

A microbenchmark can help attribute a scenario result, but it does not by itself satisfy user
value. A scenario benchmark can provide user value, but it does not by itself prove suite
completeness. Looper needs both layers with explicit relationships.

## 8. TencentBench / TenBench baseline status

Public evidence does not currently identify an official Tencent project named `TencentBench`
or `TenBench`:

- Exact GitHub and Gitee repository searches did not identify a matching Tencent benchmark.
- Exact Tencent Cloud developer-community search for `TencentBench` returned zero items on
  2026-08-21.
- Tencent Cloud CVM instance-specification documentation is an active official source for
  advertised CPU, memory, storage, bandwidth, PPS, and instance-family facts, but it is not a
  downloadable cross-provider benchmark suite.
- TencentOS, OpenCloudOS, and their kernel repositories are platform projects, not evidence of
  a suite with this name.
- CIS Tencent Cloud Foundation Benchmark is a security baseline and is not a performance
  benchmark.

Current status: `unresolved-internal-baseline`.

Until the meeting artifact, internal URL, screenshot, command, or test list is supplied, P0 may
record provisional gaps against a generic CPU/memory/storage/network suite, but must not claim
those are confirmed TencentBench gaps.

Likely provisional gaps to test after identification are workload-level SLOs, raw-result and
variance disclosure, host-placement variability, database and microservice scenarios, modern
LLM inference, agent runtime, GPU-fabric scaling, energy/cost, and cross-provider normalization.
These are hypotheses, not confirmed findings.

## 9. Cloud validation stages

- Stage A: one low-cost instance proves installation, execution, correctness, artifact capture,
  and cleanup only.
- Stage B: two instance families or generations under one controlled image/load prove initial
  discrimination.
- Stage C: recreated placements and repeated blocks estimate black-box variability.
- Stage D: cross-provider targets are added only after normalization rules and budget approval.

Creating one Tencent CVM is therefore a valid smoke-test step, but it cannot complete the
fairness or discrimination acceptance criteria.

## 10. P0 deliverables

Current status: public-candidate deliverables are recorded in `docs/p0-candidate-audit.md` and `docs/p0-benchmark-landscape.md`. The actual TencentBench identity and confirmed overlap matrix remain blocked on an internal artifact; the public-neighbor analysis in `docs/p0-tencentbench-gap.md` is not a substitute.

1. Baseline identity and coverage report for the actual TencentBench/TenBench artifact.
2. Candidate pool with immutable source and publication evidence.
3. Five-dimensional evidence vectors and explicit blockers.
4. Coverage and overlap matrix against the confirmed baseline.
5. Provisional Top 10 with implementation-ready and research-priority tracks.
6. Reproduction plan, resource estimate, and cloud comparison matrix for each selected item.
7. Review record explaining why each selected benchmark serves a user decision.

The initial public-candidate review authorized P1 Stage 0 local contract work while the internal baseline remains unresolved; this does not authorize a TencentBench gap claim or cloud resources. Later P1 stages continue to reframe execution, evidence, statistics, and cloud components around benchmark catalog, target comparison, and selection reports rather than closed-loop parameter optimization.

## 11. Primary public references

- Tencent Cloud CVM instance specifications: https://cloud.tencent.com/document/product/213/11518
- DCPerf: https://github.com/facebookresearch/DCPerf
- DeathStarBench: https://github.com/delimitrou/DeathStarBench
- MLPerf Inference: https://github.com/mlcommons/inference
- PerfKit Benchmarker: https://github.com/GoogleCloudPlatform/PerfKitBenchmarker
- SeBS: https://github.com/spcl/serverless-benchmarks
- SuperBench: https://github.com/microsoft/superbenchmark
- BenchBase: https://github.com/cmu-db/benchbase
- IO500: https://github.com/IO500/io500
