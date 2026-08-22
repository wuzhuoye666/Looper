# P0 Candidate Audit: 23 Benchmarks

Status: complete public-candidate appendix; aligned Top 10 unchanged  
Evidence cutoff: 2026-08-21  
Scoring framework: `docs/p0-benchmark-evaluation.md`  
Baseline status: `TencentBench` / `TenBench` remains `unresolved-internal-baseline`

## 1. Reading rules

This appendix audits exactly 23 candidates: the aligned Top 10 and 13 explicit exclusions. Scores are evidence levels, not a weighted quality score. The vector order is:

- `A`: availability;
- `V`: user value;
- `C`: completeness / gap fill;
- `F`: fairness;
- `D`: discrimination;
- `R`: robustness, reported separately.

No candidate receives `A=5` or `R=5` because Looper has not reproduced equivalent workload semantics across the target classes used for procurement. An `unknown` field is not inferred from a repository name or paper abstract. Public absence of `TencentBench` is not evidence of internal absence, so `C` is provisional against a generic CPU/memory/storage/network baseline, not a confirmed TencentBench gap.

A failed live revision lookup is also not evidence that a repository is absent. The 2026-08-21 `git ls-remote` pass resolved YCSB, iperf and LLMPerf below; transient GitHub connection failures left IO500, fio, HPCG and GenAI-Perf revisions as `unknown`.

## 2. Identity and supply-chain record

| ID | Candidate and official source | Exact revision / release at cutoff | Publication evidence | Code and non-code terms |
| --- | --- | --- | --- | --- |
| T1 | [MLPerf Inference](https://github.com/mlcommons/inference) | v6.1 research target; `b66003e10e2db4b8c36a448b0545ec90fcc6e9e9` | [arXiv:1911.02549](https://arxiv.org/abs/1911.02549); venue unknown in this record | Apache-2.0 code. Model weights, datasets, preprocessing, containers, closed system software and result redistribution require per-workload review. |
| T2 | [MLPerf Storage](https://github.com/mlcommons/storage) | `ce28a9888076eba5a58fdd93d1c0258bf27b7aa2`; formal release label unknown | [DOI 10.1145/3572751.3572765](https://doi.org/10.1145/3572751.3572765); venue unknown in this record | Apache-2.0 code. Training data, checkpoints, KV-cache/vector data, models, containers and submission-result terms require review. |
| T3 | [MLPerf Training](https://github.com/mlcommons/training) | v6.1 research target; `b8b3adcaa0db110a79dc19e183c22b54489b48fa` | [arXiv:1910.01500](https://arxiv.org/abs/1910.01500); venue unknown in this record | Apache-2.0 code. Models, training data, weights, containers, accelerator software and result terms require review. |
| T4 | [SuperBench](https://github.com/microsoft/superbenchmark) | `67298aef5decc64e0f67877c8916898bebc87847`; release label unknown | [arXiv:2402.06194](https://arxiv.org/abs/2402.06194), USENIX ATC 2024 | MIT code. Drivers, CUDA/ROCm, NCCL, firmware, vendor tools, containers and result terms require review. |
| T5 | [DCPerf](https://github.com/facebookresearch/DCPerf) | `9308c3e3c404e0466f0a2929f15ddcf62b2215f6`; activity snapshot 2025-09-09 | [DOI 10.1145/3695053.3731411](https://doi.org/10.1145/3695053.3731411), ISCA 2025 | MIT root license. Archive, SHA-256, byte count and root LICENSE evidence are locked. Per-workload component/data/image terms remain separate. |
| T6 | [BenchBase](https://github.com/cmu-db/benchbase) | `33c00473807ebd49304d114a6d769d2d2b2bbb34`; activity snapshot 2025-12-13 | [OLTP-Bench, PVLDB](http://www.vldb.org/pvldb/vol7/p277-difallah.pdf); DOI unknown in this record | Apache-2.0 root license. The 43,099,345-byte archive, SHA-256 and root LICENSE hash are locked. DBMS images, datasets and named workload terms require review. |
| T7 | [SeBS + SeBS-Flow](https://github.com/spcl/serverless-benchmarks) | `ef76f4278c6c7eaf1779f661bc26d5c79c7e4330`; activity snapshot 2026-08-18 | [SeBS arXiv:2012.14132](https://arxiv.org/abs/2012.14132), EuroSys 2021; [SeBS-Flow arXiv:2410.03480](https://arxiv.org/abs/2410.03480), EuroSys 2025 | BSD-3-Clause code. Language runtimes, cloud services, function dependencies, data and external APIs require separate review. |
| T8 | [DeathStarBench](https://github.com/delimitrou/DeathStarBench) | `6ecb09706140f8730b5385c08f1386c654c3c526`; activity snapshot 2024-06-27 | [DOI 10.1145/3297858.3304013](https://doi.org/10.1145/3297858.3304013), ASPLOS 2019 | GPL-2.0 code. Service components, databases, images/data and container terms require review; Looper integration/redistribution boundary needs legal review. |
| T9 | [TailBench v0.9](https://tailbench.csail.mit.edu/) + [TailBench++ candidate](https://github.com/zliUPV/Tailbenchplusplus) | Original v0.9; candidate `1a707726ddd171ebf7e2bd3db52f5f5bbe4a9c7c` dated 2026-01-27 | [DOI 10.1109/IISWC.2016.7581261](https://doi.org/10.1109/IISWC.2016.7581261), IISWC 2016 | Original package/workload/about 10 GB input terms unknown. TailBench++ is `NOASSERTION`; official relationship to original TailBench is unverified. |
| T10 | [Atrex-Bench](https://github.com/alibaba/atrex-bench) | `e09242e96b73b22d20a0411099947558e1861b4e`; activity snapshot 2026-08-20 | [arXiv:2607.14541](https://arxiv.org/abs/2607.14541); venue unknown | Apache-2.0 code; archive and NOTICE evidence locked. Trace-derived tasks, model/API, GPU software and trace terms require review. |
| X1 | [CloudSuite 4.0](https://github.com/parsa-epfl/cloudsuite) | `c9d7584b9f4f0dec56e6683ebd61dad66ac1d06a`; activity snapshot 2023-06-25 | DOI/arXiv/venue unknown in this record | Aggregate code license and each of eight stack/data/model/image terms require component-level review. |
| X2 | [PerfKit Benchmarker](https://github.com/GoogleCloudPlatform/PerfKitBenchmarker) | `946ea317692cac6c78e4aec1cd041b538f1a0285`; activity snapshot 2026-08-21 | DOI/arXiv/venue unknown | Repository and every bundled benchmark/provider/image license require review; cloud account, service and result terms are separate. |
| X3 | [NCCL Tests](https://github.com/NVIDIA/nccl-tests) | `717b68318278e93f371d8ffb46b076069d7c7851`; activity snapshot 2026-08-03 | DOI/arXiv/venue unknown | Exact-revision code license not locked in Looper. NCCL, CUDA, drivers, firmware, topology and container terms are separate. |
| X4 | [IO500](https://github.com/IO500/io500) | exact commit unknown; live lookup failed at cutoff | DOI/arXiv/venue unknown | IO500/IOR/mdtest/find and [submission-data](https://github.com/IO500/submission-data) terms require separate review; result-data rights are unverified. |
| X5 | [SHARP](https://github.com/HewlettPackard/SHARP) | tag v2.0.0, `e8dd8b577dfb467da6071b27b9b02456c35a41d9`, 2024-03-14 | [DOI 10.1109/IISWC63097.2024.00017](https://doi.org/10.1109/IISWC63097.2024.00017), IISWC 2024 | MIT code; archive/root-license evidence locked. CPU/CUDA/MPI/Fission/Knative framework, image and cloud-service terms remain separate. |
| X6 | [AgentBench](https://github.com/THUDM/AgentBench) | `d1e4a10db08c87075c78972e48ecc182be03e2d5`; activity snapshot 2026-02-08 | DOI/arXiv/venue unknown in this record | Code, task environments, data, models, API services, containers and third-party environment terms require review. |
| X7 | [YCSB](https://github.com/brianfrankcooper/YCSB) | `66302f301b13f60d4bcb2f29f478586bb1d6f2e0` resolved 2026-08-21 | DOI/arXiv/venue unknown in this record | Exact-revision code license and every database binding/data/image term are not locked in Looper. |
| X8 | [fio](https://github.com/axboe/fio) | exact commit unknown; live lookup failed at cutoff | DOI/arXiv/venue unknown | GPL-2.0 code. Job files, test data, file system/cloud-volume and result terms are scenario-specific. |
| X9 | [STREAM](https://www.cs.virginia.edu/stream/) | exact package/revision unknown | DOI/arXiv/venue unknown in this record | Code/use, compiler, binding, data-size and result-redistribution terms require review. |
| X10 | [iperf3](https://github.com/esnet/iperf) | `c9b74229d0d9bfec6d2307b66b43c29a7665ad0b` resolved 2026-08-21 | DOI/arXiv/venue unknown | BSD-3-Clause code. Network path, endpoint, cloud-service and result terms remain deployment-specific. |
| X11 | [HPCG](https://github.com/hpcg-benchmark/hpcg) | exact commit unknown; live lookup failed at cutoff | DOI/arXiv/venue unknown in this record | Exact code, MPI, input, compiler, image and result-submission terms require review. |
| X12 | [SPEC CPU2026](https://www.spec.org/cpu2026/) | CPU2026; exact kit revision unknown | DOI/arXiv/venue unknown | Commercial/proprietary kit; source and inputs are not redistributable. Compiler, run and result-reporting terms are governed by SPEC. |
| X13 | [NVIDIA GenAI-Perf](https://github.com/NVIDIA/GenAI-Perf) + [LLMPerf](https://github.com/ray-project/llmperf) | GenAI-Perf commit unknown after failed lookup; LLMPerf `f1d6bed47e4501b0e371082b41601b59ab55269f` | DOI/arXiv/venue unknown | Exact code licenses are not locked in Looper. Models, datasets, APIs, server implementations, images, cloud services and result terms require review. |

## 3. Five-dimensional evidence

| ID | Vector; R | Track | Evidence for A / V / C / F / D; robustness |
| --- | --- | --- | --- |
| T1 | `4/5/5/4/4`; R4 | `adopt-after-cost-check` | **A4:** formal rules, submissions and runnable upstream exist, but no Looper cross-target reproduction. **V5:** directly answers accuracy-constrained offline/service/streaming/GenAI inference decisions. **C5:** adds modern LLM, RAG, generation and Edge Agentic workloads. **F4:** rules control models/scenarios/results; Looper must still freeze target software and cost. **D4:** published multi-system results discriminate, but Looper effect/variance/attribution is pending. **R4:** active CPU/GPU/edge coverage, not Looper-equivalent reproduction. |
| T2 | `4/5/5/4/4`; R4 | `adopt-after-cost-check` | **A4:** code, rules and submission validation exist; minimum practical dataset and Looper path are pending. **V5:** answers training-I/O, checkpoint, KV-cache and vector storage supply questions. **C5:** fills an AI-storage gap not covered by fio. **F4:** defined workloads/rules, with backend/data scale still to normalize. **D4:** submissions compare systems, without Looper placement/effect evidence. **R4:** file/object and several AI storage modes are supported upstream. |
| T3 | `4/5/5/4/4`; R4 | `adopt-after-cost-check` | **A4:** code, quality targets and submission rules exist; no Looper real run. **V5:** time-to-quality and cost directly support AI-server purchases. **C5:** covers LLM/MoE/generative training and scaling. **F4:** quality gates are strong, while full software/cost normalization remains. **D4:** published systems differ; Looper repetitions and attribution are pending. **R4:** multi-accelerator/scale support is upstream-validated. |
| T4 | `4/4/5/4/5`; R4 | `adopt-pilot` | **A4:** executable upstream exists, but no Looper supernode reproduction. **V4:** validates GPU node/fabric health, with business impact mapping still required. **C5:** fills compute, memory, link, collective and reliability attribution gaps. **F4:** topology/driver/order must be frozen by Looper. **D5:** fine-grained anomaly and link tests provide strong discrimination/attribution. **R4:** broad GPU/network/infrastructure support upstream. |
| T5 | `3/5/4/4/4`; R4 | `adopt-pilot` | **A3:** exact archive/license and normalizer are controlled; real upstream workload has not run. **V5:** six production-inspired services directly test datacenter servers. **C4:** fills modern service/analytics/transcode coverage, with overlap. **F4:** jobs/platform facts can be fixed; component/isolation topology still needs execution proof. **D4:** multi-workload, x86_64/aarch64 evidence is promising; Looper statistics pending. **R4:** two CPU architectures and several workloads are documented. |
| T6 | `4/5/4/4/4`; R4 | `adopt-pilot` | **A4:** exact source/archive/license plus Looper adapter/runtime-normalizer smoke exist; PostgreSQL/BenchBase itself has not run. **V5:** transaction capacity under tail/error SLO directly supports DB-server selection. **C4:** adds OLTP/multi-DB coverage; YCSB overlap is managed internally. **F4:** DBMS, mix, offered load, persistence and client isolation can be frozen. **D4:** rich throughput/latency data exists; multi-target placement evidence is pending. **R4:** multi-DB JDBC framework, without Looper target parity yet. |
| T7 | `4/4/5/4/4`; R4 | `adopt-pilot` | **A4:** AWS/Azure/GCP/OpenWhisk/local Docker paths exist; Looper provider reproduction is pending. **V4:** cold/warm/burst/workflow decisions are useful, but managed-platform results are not pure hardware results. **C5:** fills serverless and workflow fan-out. **F4:** common harness exists; region/quota/external-service differences require disclosure. **D4:** latency/throughput/cost/failure metrics discriminate platforms. **R4:** multi-cloud, language and local deployment coverage. |
| T8 | `3/5/5/3/4`; R2 | `adopt-pilot` | **A3:** public workloads/exact revision exist; deployment remains complex and unrun by Looper. **V5:** end-to-end microservice RPC/tail SLO is directly useful. **C5:** adds real multi-service topology. **F3:** versions, client, network and placement controls require substantial work. **D4:** multiple services/metrics can separate systems; uncertainty/attribution is pending. **R2:** one difficult primary platform and dependency path dominate. |
| T9 | `2/5/5/3/4`; R1 | `research-priority` | **A2:** source/data are obtainable, but licenses and old dependencies block adoption; candidate reports only 7/8 Ubuntu 24 builds. **V5:** directly targets latency-critical tail behavior. **C5:** dynamic load and tail methodology fill a priority gap. **F3:** rate/method exist, but client/network/platform controls need rebuilding. **D4:** varied workloads/QPS should discriminate, without Looper evidence. **R1:** old/hard-wired platform and uncertain extension status. |
| T10 | `3/3/5/4/3`; R4 | `research-priority` | **A3:** exact archive/NOTICE are controlled, but no real Looper GPU run. **V3:** useful for agent-generated kernel quality, not server agent capacity. **C5:** trace-derived correctness and speed-of-light normalization are distinctive. **F4:** AMD/NVIDIA and correctness controls exist; model/compiler/GPU environments need freezing. **D3:** task/platform differences exist, but stable server ranking is unproven. **R4:** AMD and NVIDIA paths are represented. |
| X1 | `3/5/3/3/3`; R2 | `exclude-overlap` | **A3:** public eight-stack suite exists; no Looper run. **V5:** analytics/cache/data/graph/media/search/web are relevant. **C3:** material coverage but high DCPerf/DeathStarBench overlap. **F3:** component versions/deployment/client isolation need refreezing. **D3:** likely multi-workload differences, without Looper statistics. **R2:** architecture/port parity is unresolved. |
| X2 | `3/4/2/4/2`; R4 | `exclude-control-plane-overlap` | **A3:** mature cross-cloud automation exists. **V4:** useful for cloud execution and provider comparison. **C2:** mostly duplicates Looper orchestration rather than adding a scenario. **F4:** vendor-neutral governance is a strong method reference. **D2:** the framework itself does not guarantee workload effect/attribution. **R4:** broad provider support. |
| X3 | `3/3/2/4/4`; R3 | `exclude-companion-probe` | **A3:** runnable collective/correctness tooling exists; Looper environment is not locked. **V3:** explains GPU fabric, not end-user training/inference capacity alone. **C2:** companion to SuperBench/Training. **F4:** collective/topology/output can be controlled. **D4:** sensitive to NVLink/PCIe/RDMA/node boundaries. **R3:** single/multi-node NVIDIA/NCCL focus. |
| X4 | `2/4/3/4/4`; R2 | `exclude-scope` | **A2:** formal rules/results exist, but ordinary CVM/cloud-disk execution is not established here. **V4:** direct for HPC shared storage. **C3:** fills parallel-file-system coverage outside initial scope. **F4:** mature rules/validation, with scale/topology comparability constraints. **D4:** public multi-metric rankings discriminate. **R2:** deployment fit is narrow for current targets. |
| X5 | `2/3/2/4/3`; R3 | `exclude-method-reference` | **A2:** exact artifact is locked but not runtime-integrated. **V3:** variability/profiling informs experiments indirectly. **C2:** overlaps SeBS/SuperBench method and deployment dimensions. **F4:** statistics/profiling design is strong. **D3:** detects variability, but purchase-workload ranking is unproven. **R3:** CPU/CUDA/MPI/Fission/Knative breadth with parity gaps. |
| X6 | `2/4/2/2/2`; R2 | `exclude-object-mismatch` | **A2:** multi-environment tasks exist, with external dependencies and no Looper path. **V4:** good for agent/model capability. **C2:** lacks controlled concurrent server-runtime capacity. **F2:** external LLM/API/tool limits confound hardware comparison. **D2:** primarily separates model/agent ability. **R2:** diverse environments do not establish portable server attribution. |
| X7 | `3/3/1/3/3`; R3 | `exclude-duplicate` | **A3:** common KV workload/bindings exist; no Looper reproduction. **V3:** useful for basic KV/cloud DB selection. **C1:** BenchBase already includes YCSB. **F3:** workload controls exist, DB/client configuration remains. **D3:** KV differences are measurable but narrow. **R3:** multiple bindings, without equivalent Looper validation. |
| X8 | `3/2/1/4/3`; R3 | `exclude-attribution-probe` | **A3:** mature runnable tool, no locked Looper job protocol. **V2:** mechanism evidence, not complete workload decision. **C1:** storage probe only. **F4:** block size/iodepth/direct/runtime are controllable. **D3:** storage backends separate clearly, but business rank may not. **R3:** broad OS/backend use. |
| X9 | `2/2/1/3/3`; R2 | `exclude-attribution-probe` | **A2:** public program exists, but exact package/build is not locked. **V2:** memory-bandwidth explanation only. **C1:** mechanism probe. **F3:** compiler, NUMA, binding and frequency are major controls. **D3:** bandwidth differences are visible, without application rank proof. **R2:** traditional portability, no Looper parity. |
| X10 | `3/2/1/3/3`; R3 | `exclude-attribution-probe` | **A3:** mature tool/exact HEAD resolved; no Looper topology protocol. **V2:** network throughput evidence does not replace RPC/business SLO. **C1:** mechanism probe. **F3:** streams/path/packet behavior can be set, while cloud paths remain black-box. **D3:** bandwidth/PPS differences are visible. **R3:** broad platform/path use, no Looper topology reproduction. |
| X11 | `2/2/1/3/3`; R2 | `exclude-scope` | **A2:** public implementation/rules exist, but revision/MPI/scale are not locked. **V2:** sparse-memory/communication proxy is indirect for general server buyers. **C1:** overlaps memory/CPU attribution. **F3:** node count, MPI, binding and topology must match. **D3:** HPC systems can differ, with weak current-CVM relevance. **R2:** HPC/multi-node focus. |
| X12 | `1/4/2/4/4`; R3 | `exclude-commercial-reference` | **A1:** commercial restricted kit is not distributable by Looper. **V4:** strict CPU speed/rate evidence is useful externally. **C2:** narrow CPU coverage. **F4:** formal run/report rules are strong. **D4:** CPU generations separate, but production mapping is indirect. **R3:** broad CPU comparison, without Looper-controlled materials. |
| X13 | `2/4/2/2/3`; R2 | `exclude-identity-pending` | **A2:** request generators exist, but one revision and all supply-chain terms are not yet locked. **V4:** LLM throughput and first/per-token latency matter. **C2:** overlaps MLPerf Inference service scenarios. **F2:** model/API/request/server controls are not standardized here. **D3:** online systems may separate, but accuracy/repeat/effect evidence is incomplete. **R2:** multiple service directions, no equivalent cross-target proof. |

## 4. Ordered Top 10 and explicit exclusions

The ordered Top 10 remains:

1. MLPerf Inference v6.1
2. MLPerf Storage
3. MLPerf Training v6.1
4. SuperBench
5. DCPerf
6. BenchBase
7. SeBS + SeBS-Flow
8. DeathStarBench
9. TailBench v0.9 + TailBench++ candidate
10. Atrex-Bench

The order protects coverage and the meeting-specified Agent Runtime/GPU-supernode directions before considering score sums. It is not an arithmetic leaderboard. TailBench and Atrex-Bench remain research-priority, not immediate integrations. AgentBench is task/capability material, while Atrex-Bench is kernel-generation material; neither is silently relabeled as a server Agent Runtime benchmark. SuperBench owns the GPU-supernode validation layer and NCCL Tests remains a companion attribution probe.

The 13 exclusions are explicit:

- CloudSuite: high overlap with DCPerf/DeathStarBench and component-term burden.
- PerfKit Benchmarker: control-plane/provider-method overlap with Looper.
- NCCL Tests: attribution probe, not a complete user scenario.
- IO500: valuable HPC shared-storage scope, but low fit for initial general CVM targets.
- SHARP: variability/profiling method reference rather than a procurement workload.
- AgentBench: evaluates agent/model capability; external services confound server attribution.
- YCSB: materially duplicated inside BenchBase.
- fio, STREAM and iperf3: storage, memory and network attribution probes.
- HPCG: HPC proxy outside the initial default suite.
- SPEC CPU2026: commercial external evidence, not a redistributable default integration.
- GenAI-Perf / LLMPerf: identity/supply-chain/rules need locking and the use case overlaps MLPerf Inference.

## 5. Diff against the aligned Top 10

| Position | Aligned candidate | Audit result | Diff |
| --- | --- | --- | --- |
| 1 | MLPerf Inference v6.1 | Same member, vector `4/5/5/4/4`, R4, `adopt-after-cost-check` | No rank/score/track change; supply-chain unknowns are explicit. |
| 2 | MLPerf Storage | Same member, vector `4/5/5/4/4`, R4, `adopt-after-cost-check` | No rank/score/track change; minimum scale/data terms remain gates. |
| 3 | MLPerf Training v6.1 | Same member, vector `4/5/5/4/4`, R4, `adopt-after-cost-check` | No rank/score/track change; model/data/cost gates retained. |
| 4 | SuperBench | Same member, vector `4/4/5/4/5`, R4, `adopt-pilot` | No change; D5 does not erase the business-workload boundary. |
| 5 | DCPerf | Same member, vector `3/5/4/4/4`, R4, `adopt-pilot` | No rank change; exact archive/license and normalizer evidence added, but A remains 3 until a real run. |
| 6 | BenchBase | Same member, vector `4/5/4/4/4`, R4, `adopt-pilot` | No rank change; archive/license, source semantics and normalizer evidence are now locked. |
| 7 | SeBS + SeBS-Flow | Same member, vector `4/4/5/4/4`, R4, `adopt-pilot` | No change; managed-platform attribution warning retained. |
| 8 | DeathStarBench | Same member, vector `3/5/5/3/4`, R2, `adopt-pilot` | No change; GPL/deployment/isolation risks remain visible. |
| 9 | TailBench family | Same member, vector `2/5/5/3/4`, R1, `research-priority` | No change; licensing, old dependencies and extension status still block adoption. |
| 10 | Atrex-Bench | Same member, vector `3/3/5/4/3`, R4, `research-priority` | No change; it remains kernel-agent material, not server Agent Runtime capacity. |

Summary: no member, order, score or track changed. The appendix adds immutable identity where evidence exists, records unknowns instead of inference, expands the previously grouped microbenchmark exclusion into fio/STREAM/iperf3/HPCG records, and preserves the requirement for a separately designed Looper Agent Runtime scenario.

## 6. Primary sources

- MLPerf Inference: https://github.com/mlcommons/inference
- MLPerf Training: https://github.com/mlcommons/training
- MLPerf Storage: https://github.com/mlcommons/storage
- SuperBench: https://github.com/microsoft/superbenchmark
- DCPerf: https://github.com/facebookresearch/DCPerf
- BenchBase: https://github.com/cmu-db/benchbase
- SeBS: https://github.com/spcl/serverless-benchmarks
- DeathStarBench: https://github.com/delimitrou/DeathStarBench
- TailBench: https://tailbench.csail.mit.edu/
- TailBench++ candidate: https://github.com/zliUPV/Tailbenchplusplus
- Atrex-Bench: https://github.com/alibaba/atrex-bench
- Remaining official URLs are recorded in the identity table above.
