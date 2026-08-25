# Phoronix Test Suite / PHPBench Looper Package

This package is the first audited PTS adapter pilot. It deliberately exposes one
immutable OpenBenchmarking profile: `pts/phpbench-1.1.6`.

PTS is a benchmark framework, not a single benchmark contract. Profiles such as
FIO, Blender, Apache, and MPI have different units, machine roles, dependencies,
and safety constraints. Add them as separate versioned Looper packages; do not
turn the profile id into a free-form run parameter.

The producer:

1. resolves `phoronix-test-suite` from `LOOPER_PTS_BIN` or `PATH`;
2. optionally prefixes it with `LOOPER_PHP_BIN` for a source checkout;
3. runs `default-benchmark pts/phpbench-1.1.6` with a version-scoped reusable PTS user directory;
4. disables interactive result naming, automatic index refresh, and
   OpenBenchmarking uploads in the pinned configuration;
5. exports the saved result through PTS `result-file-to-json`.

The normalizer only emits a score when the result identifier, `Score` scale,
`HIB` direction, finite aggregate, and finite raw samples all match the package
contract. Raw PTS JSON is kept as `pts-result.json`.

Version `looper12` is executable through Looper's managed local-process
provisioning contract. A clean Linux Worker only needs Python, the Worker
runtime, network access during first preparation, and root/passwordless sudo.
After the user selects a target and starts the experiment, Looper delivers this
complete package, runs `prepare.py`, installs PHP CLI/unzip when missing,
downloads and verifies the pinned PTS source, seeds the 32 KiB PHPBench payload
that is bundled in this Git package, and reuses the
version-scoped dependency cache, OpenBenchmarking indexes, and installed test
state on later attempts. A completed-runtime marker lets repeated leases skip
the expensive PTS startup/version probe. The downloader streams in
fixed chunks, retries transient failures with backoff, falls back to the
equivalent codeload archive, and keeps the SHA-256 check as a hard gate. On
cloud targets with slow egress a mirror can be injected via
`LOOPER_PTS_ARCHIVE_URL`. The producer streams
PTS stdout/stderr live with stdin closed and silent mode enabled, so install
prompts or stalled downloads surface in the terminal instead of being buffered
until timeout. Final registration of the ZIP is the explicit approval for this
trusted local-process package.
