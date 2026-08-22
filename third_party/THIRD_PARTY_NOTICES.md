# Third-Party Notices

This repository records the following upstream projects for dependency governance,
optional integrations, benchmark discovery, or result-format compatibility. No
upstream source or benchmark data is included by these records. The adapter fixtures
under `adapters/` are small, original, synthetic examples and are not copied from an
upstream project.

License names below are governance metadata, not a substitute for the upstream
license text or legal review. Before fetching, vendoring, modifying, or distributing
an upstream, pin an exact revision and retain all notices required by that revision.

| Upstream | Role | License status | Inclusion status | URL |
| --- | --- | --- | --- | --- |
| optuna/optuna | Dependency | MIT, verified | Approved dependency | https://github.com/optuna/optuna |
| sosy-lab/benchexec | Optional integration | Apache-2.0, verified | Approved optional | https://github.com/sosy-lab/benchexec |
| facebookresearch/DCPerf | Benchmark source | MIT, verified | Pinned and downloaded to ignored cache | https://github.com/facebookresearch/DCPerf |
| alibaba/atrex-bench | Benchmark source | Apache-2.0, verified | Pinned and downloaded to ignored cache | https://github.com/alibaba/atrex-bench |
| HewlettPackard/SHARP | Benchmark analysis source | MIT, verified at `v2.0.0` | Pinned and downloaded to ignored cache; not runtime-integrated | https://github.com/HewlettPackard/SHARP |
| Variability-Guided Performance Optimization | Optimizer paper reference | Paper license unverified | Metadata-only; no independent code release asserted | https://doi.org/10.1145/3777884.3796994 |
| CMU-SAFARI/Cleaning-up-the-Mess | Benchmark catalog | MIT, verified | Catalog-only | https://github.com/CMU-SAFARI/Cleaning-up-the-Mess |
| cornell-sysphotonics/ccl-bench | Workload reference | License unverified | Reference-only | https://github.com/cornell-sysphotonics/ccl-bench |
| Rucchao/CloudyBench2024 | Benchmark catalog | License unverified | Blocked pending license review | https://github.com/Rucchao/CloudyBench2024 |
| chenzhi-cz/performance-optimization-benchmark-reliability | Benchmark catalog | License unverified | Blocked pending license review | https://github.com/chenzhi-cz/performance-optimization-benchmark-reliability |
| IO500/submission-data | Result catalog | License unverified | Blocked pending license review | https://github.com/IO500/submission-data |
| TailBench v0.9 | Official benchmark-suite reference | Package, workload, and data terms unverified | Reference only; automatic source and dataset download blocked | https://tailbench.csail.mit.edu/ |
| zliUPV/Tailbenchplusplus | Candidate benchmark catalog | NOASSERTION; official association unverified | Blocked pending license and provenance review | https://github.com/zliUPV/Tailbenchplusplus |
| bsc-mem/Mess-benchmark | Benchmark catalog | BSD-3-Clause, verified | Too large; catalog-only | https://github.com/bsc-mem/Mess-benchmark |
| SPEC CPU2026 | Commercial benchmark reference | Proprietary; non-redistributable | Reference-only; no redistributable source | https://www.spec.org/cpu2026/ |

## Important restrictions

- `license-unverified` and `NOASSERTION` entries are not approved for source or data
  ingestion, vendoring, or redistribution.
- Catalog-only entries contribute metadata and links only. Their source trees and
  datasets are intentionally absent.
- SPEC CPU2026 materials are not redistributable through this repository. Obtain and
  use them only under the terms supplied by SPEC.
- A repository-level license may not cover every dataset, model, submodule, or bundled
  artifact. Review the exact pinned revision before use.
- DCPerf and Atrex-Bench have live-resolved commits and verified archive digests in `sources.lock.yaml`; every unresolved repository remains `commit: null` with an explicit resolution status.
