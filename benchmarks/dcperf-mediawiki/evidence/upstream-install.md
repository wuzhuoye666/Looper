# DCPerf MediaWiki Runtime Evidence

This package executes the pinned DCPerf workload rather than the synthetic adapter fixture.

- DCPerf source: commit 9308c3e3c404e0466f0a2929f15ddcf62b2215f6, MIT licensed.
- Selected job: oss_performance_mediawiki_mlp.
- Workload topology: one Ubuntu host co-locates HHVM, nginx, MariaDB, memcached, wrk, and the client load generator.
- Runtime compatibility is intentionally restricted to Ubuntu 22.04 x86_64. Ubuntu 24.04 and other distributions are rejected by prepare.py because the pinned HHVM 3.30 appliance is not supported there.
- The native Benchpress JSON reporter remains in benchpress-result.json; the package normalizer derives Looper metrics without replacing that file.
- The upstream MediaWiki archive is bundled in the locked oss-performance source archive under targets/mediawiki/mediawiki-1.28.0.tar.gz.
- The current fixture under adapters/dcperf-mediawiki/fixture is synthetic parser evidence only and is never used by the executable package.

A real smoke run must use a target that satisfies the manifest capability gate and the host checks. The currently registered Ubuntu 24.04 target is deliberately not claimed as a compatible smoke target.
