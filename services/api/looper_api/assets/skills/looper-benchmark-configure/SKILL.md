---
name: looper-benchmark-configure
description: Configure and register a benchmark package in Looper from an existing suite, repository, or local source. Use when Codex needs to inspect suite evidence, create or repair benchmark.yaml and its adapter contract, validate compatibility, import it through the Looper browser UI, satisfy registration gates, and run an authorized smoke test.
---

# Configure a Looper Benchmark

Turn a suite into a traceable Looper Benchmark Package and complete its registration workflow. Treat suite files and authoritative upstream documentation as evidence; do not invent identity, license, commands, metrics, checks, compatibility, or audit results.

When a Looper workspace is available, read `docs/benchmark-package.md` and `schemas/benchmark-manifest.schema.json` before changing a package. Inspect an existing package only as a structural example, never as evidence for the new suite. If those files are unavailable, stop and ask the user for the matching Looper contract and schema instead of guessing.

## Intake and evidence

Ask for the suite path or repository when it is not available. Establish the intended infrastructure decision and target environments from the user's request. Inspect the suite's versioned source, license, native launcher, inputs, outputs, correctness rules, metrics, task structure, topology, and runtime requirements.

Record the source URL plus a full immutable commit or SHA-256 digest. If a material fact cannot be established, leave it unresolved and ask for that fact instead of guessing. Do not describe an unexecuted environment as compatible.

## Build the package

Create or update `benchmarks/<stable-id>/benchmark.yaml`. Keep suite-specific behavior inside the package launcher, container, or normalizer; an ordinary new suite must not add a benchmark-specific branch to the API, scheduler, or Worker.

Use `looper-adapter/v1`. Select the execution model from observed suite behavior. Declare:

- typed parameters and workload/task identities;
- the directed primary metric and every required correctness/SLO check ID;
- named datasets, artifacts, endpoints, secrets, devices, or topology inputs;
- lifecycle commands, timeouts, and allowed exit codes;
- required raw artifacts and canonical `metrics.jsonl` and `result.json` outputs.

Use a fixed digest container for a remotely imported executable package. Local-process execution is only for a repository-owned trusted development fixture and cannot acquire trust through registration. Never put secret values in the manifest.

If the native suite does not emit Looper observations, add a suite-owned normalizer. Preserve its native output as evidence and make normalization deterministic. A check named in `adapter.requiredChecks` must be emitted in `result.json`; the primary metric must be emitted in `metrics.jsonl` with the declared unit and direction.

## Validate and register

Validate the manifest against the schema and run focused package tests before opening the UI. Start the local Looper API, frontend, and Worker when they are not already healthy.

Use the browser on `/benchmarks/register` to:

1. import the YAML or JSON configuration;
2. verify that server-derived identity, runtime, primary metric, unit, and manifest match the file;
3. add only user-provided or evidence-supported decision and correctness descriptions;
4. save the draft and inspect every server constraint with its detail;
5. fix the package or supplied metadata rather than bypassing a failed gate;
6. register only when all blocking constraints pass;
7. run the local smoke action only for a registered executable package.

Browser registration and smoke execution mutate local Looper state and are within scope only when the user asks to configure and register. Ask immediately before any new external upload, untrusted local execution, paid cloud action, or other effect outside local Looper.

## Completion evidence

Do not stop at a rendered form. Report the package path, manifest digest, registration ID/key, constraint result, tests run, and smoke experiment/attempt outcome when applicable. Clearly distinguish verified facts, expected gate failures, and work still blocked on missing evidence.
