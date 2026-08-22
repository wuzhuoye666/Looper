# Stage 0 Local Acceptance Report

Status: implemented locally, pending Stage 1 runtime images and explicit cloud authorization  
Evidence cutoff: 2026-08-21  
Cloud spend in this stage: CNY 0

## Accepted scope

Stage 0 implements the local control-plane and evidence contracts approved for the CPU pilot. It does not claim that BenchBase or DCPerf can run end to end, and it does not authorize Tencent Cloud resource creation.

| Requirement | Result | Evidence |
| --- | --- | --- |
| Selection and optimization are distinct modes | Pass | `ExperimentMode`, mode-specific validation, legacy optimization default |
| Scenario contract covers roles, goodput, SLO, tail and load search | Pass | `ScenarioBenchmarkSpec` and benchmark schema |
| Target binding records variant and placement identity | Pass | `SelectionDesign`, `TargetBindingSpec` |
| Price is immutable evidence, not mutable catalog state | Pass | `PriceSnapshot` and quote digest linkage |
| BenchBase source archive and license are immutable evidence | Pass | 43,099,345-byte pinned archive and root LICENSE SHA-256 recorded in source lock |
| BenchBase SmallBank adapter counts committed work only | Pass | fixture reconciliation excludes abort/retry/error/timeout and retains raw latency |
| Planned load is distinct from observed BenchBase throughput | Pass | client-load sidecar closes planned/offered/started/completed/timeout accounting and reconciles the upstream window |
| DCPerf MediaWiki adapter reports successful closed-loop RPS | Pass | successful requests divided by wall time; upstream Wrk RPS is diagnostic |
| Tail evidence carries the declared aggregates and sample count | Pass | BenchBase raw p50/p95/p99/p99.9/max, DCPerf upstream p50/p95/p99, separate timeout count, completion sample gate |
| Raw/histogram evidence is a first-class artifact | Pass | schema and worker protocol `histogram` role; required manifest outputs |
| SLO frontier is a bracket, not a finite-grid maximum | Pass | pass/fail classification, monotonicity checks, next-load planner, interval comparison |
| Normalized BenchBase evidence feeds frontier blocks | Pass | standard output maps to correctness/resource gates, latency samples, timeout/lag/headroom and bracket decision |
| Paired repeat statistics preserve pair identity | Pass | `paired_bootstrap_improvement` |
| Placement repeats are not pseudoreplicated | Pass | within-placement aggregation plus cluster-paired bootstrap |
| System fingerprint is shared and versioned | Pass | `looper.system-fingerprint/v1alpha1` used by API seed and worker |
| Scenario catalog is visible in Web/API | Pass | BenchBase and DCPerf seeded with scenario metadata and execution status |
| Adapter-only studies can be drafted | Pass | selection creation accepts adapter-only benchmark and inventory target |
| Adapter-only studies cannot start | Pass | start/resume readiness gate fails on `stage0-adapter-only` |
| Normalization-only commands emit the worker contract | Pass | installed BenchBase/DCPerf entrypoints emit metrics, result, normalized result, logs and declared raw artifacts; failures close with evidence |
| Container execution requires immutable image digest | Pass | start gate requires `@sha256:` |
| Selection scheduling balances paired order | Pass | seeded placement grouping and per-repeat rotation |
| Run envelope carries pairing and scenario identity | Pass | scenario, selection, target binding and stable `timeBlockId` extensions |
| Selection analysis is target/comparison oriented | Pass | target blocks, invalid counts, paired interval, inference unit, conclusion strength |
| Legacy compression optimizer remains usable | Pass | optimization candidate/Pareto path retained and regression tested |

## Installed scenarios

### BenchBase SmallBank + PostgreSQL 16

- Upstream commit: `33c00473807ebd49304d114a6d769d2d2b2bbb34`
- Decision metric: committed TPS under p99/error/abort SLO
- Topology: client-server with target database and fixed load generator
- Runtime status: `stage0-adapter-only`
- Local evidence: upstream-shaped summary, transaction histogram, raw latency and client-load-accounting fixtures; installed normalization-only command

### DCPerf MediaWiki

- Upstream commit: `9308c3e3c404e0466f0a2929f15ddcf62b2215f6`
- Decision metric: successful requests/second for the single-VM closed-loop full stack
- Topology: single-VM closed loop with load-generator, web service and database work included in score
- Runtime status: `stage0-adapter-only`
- Local evidence: upstream-shaped Benchpress result fixture and installed normalization-only command

## Statistical claims allowed at Stage 0

Stage 0 validates algorithms and data boundaries with synthetic fixtures. It makes no performance claim about S9, SA9, Intel, AMD, PostgreSQL capacity, or DCPerf capacity.

Future run labels remain constrained:

- one target: availability only;
- one placement pair: provisional paired result;
- three placement pairs in the approved CPU pilot: exploratory SKU evidence;
- no processor-vendor or instance-family extrapolation.

## Verification

Verification completed from the repository root:

- Python: `93 passed` with `.venv\Scripts\python.exe -m pytest` at the current Stage 0 implementation checkpoint.
- Web: `11 passed` across 2 files with `pnpm --filter looper-web test`.
- TypeScript/Vite: production build passed with `pnpm --filter looper-web build`.
- Static analysis: the full repository passed `.venv\Scripts\python.exe -m ruff check .`.
- Migration: a blank SQLite database upgraded from Alembic base through `b7e91d42c5fa`; `queue_sequence` and `ix_attempt_claim` were inspected after upgrade.
- Source governance smoke: BenchBase fetch recorded archive/license hashes; a second fetch returned `cache_hit: true` only after digest and byte-size verification.
- Runtime smoke: both installed normalization-only entrypoints completed against their tagged synthetic fixtures and emitted standard results/artifacts; a real API draft was also created with the BenchBase scenario and `POST /start` returned HTTP 409 at the `stage0-adapter-only` boundary.
- Frontier integration: normalized BenchBase output was converted to repeated block evidence; the undersampled fixture was correctly classified as confirmed fail with a lower-bracket next point.
- Visual smoke: desktop and mobile-width screenshots verified the dashboard, creation form and selection detail layout.

Tests cover manifest contracts, both adapters, frontier behavior, paired and cluster-paired inference, fingerprints, selection draft/start boundaries, persistent balanced scheduling, minimum-sample completion gates, selection API analysis, worker fencing, and legacy optimizer behavior.

## Deferred Stage 1 work

The following items are deliberately not represented as complete:

1. Build reproducible BenchBase/PostgreSQL and DCPerf MediaWiki runtime images from the pinned commits.
2. Record image SBOMs and immutable `sha256` digests in manifests.
3. Implement the container or remote runner capabilities declared by each scenario.
4. Add upstream workload launchers that generate the inputs consumed by the tested normalization-only commands.
5. Exercise adaptive SLO bracket/binary search through the scheduler against a real workload; the local core currently plans the next point but does not enqueue it.
6. Validate fixed-client isolation, network path, CPU topology and placement metadata on every target.
7. Conduct Stage A availability only after explicit budget/resource confirmation.
8. Conduct Stage B/C comparison only after the prior gate is accepted.

## Cloud authorization boundary

No Tencent Cloud instance, VPC, subnet, security group, image, disk or other billable resource was created or modified. Earlier API calls were limited to inventory, recommendation, image lookup and price inquiry. The Stage A/B/C limits in `docs/cpu-pilot-design.md` are proposals, not authorization.
