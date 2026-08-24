# DCPerf Ubuntu Multi-Version Handoff (2026-08-24)

## Current State

- Current registered catalog version: `dcperf.mediawiki.closed-loop@2026.08-pilot12`.
- Ubuntu 22.04 remains established. Ubuntu 24.04 is still a compatibility candidate; no native smoke has completed successfully.
- Target: `cloud:alibaba:cn-guangzhou:i-7xvcxcry1ejfjcd1d7ig` (`测试机1`), Ubuntu 24.04.4, x86_64, 8 vCPU, 14.72 GiB, online/runnable.
- API is reachable at `http://127.0.0.1:8000`; the remote Worker is deployed with reverse-tunnel transport.
- Pilot12 experiment `exp_ad47376b6c6947a29c22f769cb69af66` reached `managed DCPerf environment is ready` and `running pinned Benchpress job`, then remained in `running-benchmark` beyond the producer timeout window. It was cancelled, but the attempt still needs control-plane cleanup.

## Pilot Findings

- Pilot10 `exp_50a5a5c04db84bed883c6996d0eadd68`: preparation succeeded; native Benchpress failed before execution because `/usr/bin/python3` could not import `numpy`.
- Pilot11 `exp_7b4fc090e73c40dbb6d4756b5783516f`: after the NumPy lock change, native Benchpress failed because `pandas` was also missing.
- Pilot12: dependency lock now includes `python3-numpy` and `python3-pandas`; both imports were verified on the target (`numpy 1.26.4`, `pandas 2.1.4`). Benchpress stderr was empty and stdout was produced, so the previous import failure is fixed. The native workload did not produce artifacts or return a terminal attempt state.

## Current Dependency Lock

- Pilot12 dependency lock digest: `sha256:1c52ec4d7a403b9e02422133dc555648312adcfa4532748d93b26091d0f18635`.
- Stable cache: `/root/.looper-worker/work/dependency-cache/dcperf.mediawiki.closed-loop/1c52ec4d7a403b9e02422133dc555648312adcfa4532748d93b26091d0f18635`.
- HHVM 3.30.12 runs on Ubuntu 24.04 with `LD_LIBRARY_PATH=/opt/local/hhvm-3.30/lib`; the verified 2,081,192,140-byte asset is reused.
- The cache was manually migrated between pilot keys during diagnosis. Its readiness marker was corrected to the current cache root. New targets must use the normal lock-digest cache path and run the full `prepare.py` flow.

## Next Investigation

1. Clean up the cancelled pilot12 attempt and verify the Worker has no orphaned producer/native process.
2. Capture `benchmark.log`, `benchpress.stdout.log`, and `benchpress.stderr.log` from a completed or forcibly terminated attempt.
3. Reproduce the native command manually on the same target with the same 45-second parameters, checking nginx, MariaDB, memcached, HHVM, and wrk separately.
4. Do not update `evidence/upstream-install.md` or claim Ubuntu 24.04 support until the native command returns metrics and all required artifacts pass normalize/validate.

## Verification Already Completed

- HHVM archive download resume, mirror fallback, SHA-256, extraction, installation, and bundled ICU runtime verification passed on Ubuntu 24.04.
- Managed provisioning passed through apt dependencies, source/build steps, Composer, MariaDB setup, and readiness marker creation.
- `prepare.py` includes `python3-numpy` and `python3-pandas`; Ruff, Python compilation, and manifest registration passed for pilot12.
- API attempt lease is configured to 3600 seconds for first-time provisioning.

## Important Notes

- Pilot10 and pilot11 failures were missing declared runtime dependencies, not evidence of Ubuntu incompatibility. Pilot12 is the first run to reach native execution after those fixes, but it is not a successful compatibility result.
- Existing user changes in cloud, terminal, and frontend files must remain untouched.
- The next version must be immutable: any code or dependency change requires pilot13 or later.
