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
3. runs `default-benchmark pts/phpbench-1.1.6` with a private PTS user directory;
4. disables interactive result naming and relies on a fresh configuration where
   automatic OpenBenchmarking upload is off;
5. exports the saved result through PTS `result-file-to-json`.

The normalizer only emits a score when the result identifier, `Score` scale,
`HIB` direction, finite aggregate, and finite raw samples all match the package
contract. Raw PTS JSON is kept as `pts-result.json`.

Version `looper6` is executable through Looper's managed local-process
provisioning contract. A clean Linux Worker only needs Python, the Worker
runtime, network access during first preparation, and root/passwordless sudo.
After the user selects a target and starts the experiment, Looper delivers this
complete package, runs `prepare.py`, installs PHP CLI/unzip when missing,
downloads and verifies the pinned PTS source and PHPBench payload, and reuses the
version-scoped dependency cache on later attempts. Final registration of the ZIP
is the explicit approval for this trusted local-process package.
