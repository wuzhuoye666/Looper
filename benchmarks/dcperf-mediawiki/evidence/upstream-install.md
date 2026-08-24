# DCPerf MediaWiki Runtime Evidence

This package executes the pinned DCPerf workload rather than the synthetic adapter fixture.

- DCPerf source: commit 9308c3e3c404e0466f0a2929f15ddcf62b2215f6, MIT licensed.
- The 2 GiB HHVM asset may use dependency-lock transport mirrors for availability; byte count and the official release SHA-256 remain mandatory before extraction.
- Selected job: oss_performance_mediawiki_mlp.
- Workload topology: one Ubuntu host co-locates HHVM, nginx, MariaDB, memcached, wrk, and the client load generator.
- Runtime compatibility is established for Ubuntu 22.04 x86_64. Ubuntu 24.04 x86_64 is an explicit compatibility candidate: managed provisioning accepts it, but formal support requires a successful native smoke run. Other distributions and releases are rejected by prepare.py.
- The native Benchpress JSON reporter remains in benchpress-result.json; the package normalizer derives Looper metrics without replacing that file.
- The upstream MediaWiki archive is bundled in the locked oss-performance source archive under targets/mediawiki/mediawiki-1.28.0.tar.gz.
- The current fixture under adapters/dcperf-mediawiki/fixture is synthetic parser evidence only and is never used by the executable package.

A real smoke run must use a target that satisfies the manifest capability gate and the host checks. The currently registered Ubuntu 24.04 target is deliberately not claimed as a compatible smoke target.
