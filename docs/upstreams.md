# Upstream Source Governance

Third-party source decisions are recorded in `third_party/sources.lock.yaml`. The file
is a governance lock, not a package-manager lock: it records identity, URL, license
status, allowed use, and revision resolution state even when no source is vendored.
`third_party/THIRD_PARTY_NOTICES.md` provides the corresponding human-readable notice.

BenchBase, DCPerf and Atrex-Bench were resolved from live GitHub metadata and downloaded as verified archives under the ignored `.looper/upstreams` cache. No upstream source is vendored in this repository. All adapter fixtures remain original and synthetic.

## Status model

| Status | Meaning |
| --- | --- |
| `approved-dependency` | May be resolved and used as a dependency after pinning and notice review. |
| `approved-optional` | May support an optional integration after pinning and notice review. |
| `approved-fetchable` | Source may be fetched for approved benchmark work after pinning and license review at that revision. |
| `metadata-only` | Keep catalog metadata and the upstream link; do not vendor source or datasets. |
| `blocked-pending-license-review` | Do not fetch, ingest, vendor, or redistribute until the applicable license is established. |
| `reference-only` | Link and compatibility research only; no source inclusion is approved. |

`fetchable` describes policy eligibility, not evidence that a checkout occurred.
`catalog-only`, `reference-only`, and `too-large-catalog-only` entries must remain
external. `no-redistributable-source` marks material that cannot be supplied by this
repository.

## Revision resolution

Resolution is per entry. BenchBase, DCPerf and Atrex-Bench record live-resolved 40-character commits, downloaded archive SHA-256 and byte counts, and root LICENSE/NOTICE evidence hashes from those exact archives. `source fetch` inspects the archive before setting `downloaded-and-verified`, fails when no bounded root license file exists, and reuses a cache only when digest and size match the lock. Entries that were not live-resolved keep `commit: null` and `resolution_status: not-live-resolved`. Do not invent or infer a commit SHA from a branch name, release label, archive URL, or documentation page.

Before using a fetchable source:

1. Resolve the exact upstream repository and commit from a live source.
2. Recheck the license and notices at that exact revision, including submodules,
   datasets, models, and generated assets.
3. Record the immutable commit and move `resolution_status` through `live-resolved` to `downloaded-and-verified` when an archive is fetched.
4. Preserve required copyright, license, and NOTICE material.
5. Review updates as new third-party changes rather than silently moving a pin.

## Source catalog

| Source | Governance role | License and availability |
| --- | --- | --- |
| [optuna/optuna](https://github.com/optuna/optuna) | Dependency | Verified MIT; approved dependency; revision unresolved. |
| [sosy-lab/benchexec](https://github.com/sosy-lab/benchexec) | Optional integration | Verified Apache-2.0; approved optional; revision unresolved. |
| [cmu-db/benchbase](https://github.com/cmu-db/benchbase) | Benchmark source | Verified root Apache-2.0 license; downloaded and SHA-256 verified at `33c00473807ebd49304d114a6d769d2d2b2bbb34`. |
| [facebookresearch/DCPerf](https://github.com/facebookresearch/DCPerf) | Benchmark source | [Paper DOI](https://doi.org/10.1145/3695053.3731411); verified root MIT license; downloaded and SHA-256 verified at `9308c3e3c404e0466f0a2929f15ddcf62b2215f6`. |
| [alibaba/atrex-bench](https://github.com/alibaba/atrex-bench) | Benchmark source | Verified Apache-2.0; downloaded and SHA-256 verified at `e09242e96b73b22d20a0411099947558e1861b4e`. |
| [HewlettPackard/SHARP](https://github.com/HewlettPackard/SHARP) | Benchmark analysis source | [SHARP paper](https://doi.org/10.1109/IISWC63097.2024.00017); verified MIT at tag `v2.0.0`, commit `e8dd8b577dfb467da6071b27b9b02456c35a41d9`; archive and root license SHA-256 verified in the ignored cache. Cataloged but not runtime-integrated. |
| [Variability-Guided Performance Optimization](https://doi.org/10.1145/3777884.3796994) | Optimizer paper reference | DOI and 2026-05-03 publication metadata verified; paper license and independent code release unverified, metadata only. SHARP `main` is not treated as a VGO release. |
| [CMU-SAFARI/Cleaning-up-the-Mess](https://github.com/CMU-SAFARI/Cleaning-up-the-Mess) | Catalog | Verified MIT; metadata only. |
| [cornell-sysphotonics/ccl-bench](https://github.com/cornell-sysphotonics/ccl-bench) | Workload reference | License unverified; reference only. |
| [Rucchao/CloudyBench2024](https://github.com/Rucchao/CloudyBench2024) | Catalog | License unverified; blocked pending review. |
| [chenzhi-cz/performance-optimization-benchmark-reliability](https://github.com/chenzhi-cz/performance-optimization-benchmark-reliability) | Catalog | License unverified; blocked pending review. |
| [IO500/submission-data](https://github.com/IO500/submission-data) | Result catalog | License unverified; blocked pending review. Dataset rights may differ from repository metadata. |
| [TailBench v0.9](https://tailbench.csail.mit.edu/) | Official benchmark-suite reference | [Paper DOI](https://doi.org/10.1109/IISWC.2016.7581261); official source and dataset links verified, but package/workload/data terms unverified. The source is about 145 MB and dataset about 10.23 GB; blocked from automatic download. No active canonical GitHub repository is asserted. |
| [zliUPV/Tailbenchplusplus](https://github.com/zliUPV/Tailbenchplusplus) | Candidate catalog | Repository and self-description as an expanded networked TailBench variant verified; official TailBench++ status and relationship to the original suite are unverified. `NOASSERTION`; blocked pending review. |
| [bsc-mem/Mess-benchmark](https://github.com/bsc-mem/Mess-benchmark) | Catalog | Verified BSD-3-Clause; too large and catalog-only. |
| [SPEC CPU2026](https://www.spec.org/cpu2026/) | Commercial reference | Proprietary; no redistributable source. |

## Adapter fixtures

The adapter fixtures are deliberately small, original, and synthetic. They contain
no upstream benchmark source, published result data, or copied documentation.

| Fixture | Purpose | Governance boundary |
| --- | --- | --- |
| `adapters/benchbase-smallbank/` | Summary, outcome histogram, raw latency and client-load-accounting normalization. | Upstream-shaped synthetic evidence only; every emitted observation is tagged synthetic. |
| `adapters/dcperf-mediawiki/` | MediaWiki Benchpress result normalization and successful-request accounting. | Upstream-shaped synthetic evidence only; every emitted observation is tagged synthetic. |
| `adapters/dcperf-benchpress/` | Benchpress-like JSON result and normalized metric mappings. | Format example only; verify actual output at a pinned DCPerf revision. |
| `adapters/atrex/` | `eval_result.json`-like evaluation payload and score mappings. | Format example only; verify actual output at a pinned Atrex Bench revision. |
| `adapters/ccl-workload-card/` | CCL-style YAML workload description and parameter mappings. | Original reference-only example; upstream license remains unverified. |

Each adapter directory contains a README, a machine-readable manifest, and a minimal fixture set. The scenario adapters additionally expose tested normalization-only entrypoints; legacy mapping manifests use JSONPath-like notation for documentation and do not promise compatibility with every upstream version.

## License caveats

A repository license does not necessarily cover separately sourced workloads,
submission data, trained models, firmware, container images, or commercial benchmark
kits. In particular, do not redistribute SPEC CPU2026 materials, do not ingest sources
with an unverified license or NOASSERTION, and do not treat catalog-only entries as
permission to mirror their contents.
