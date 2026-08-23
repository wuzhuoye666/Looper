---
name: looper-benchmark-configure
description: Configure and register a benchmark package in Looper from an existing suite, repository, or local source. Use when Codex needs to inspect suite evidence, create or repair benchmark.yaml and its adapter contract, validate compatibility, import it through the Looper browser UI, satisfy registration gates, and run an authorized smoke test.
---

# Configure a Looper Benchmark

Turn a suite into a traceable Looper Benchmark Package and complete its registration workflow. Treat suite files and authoritative upstream documentation as evidence; do not invent identity, license, commands, metrics, checks, compatibility, or audit results.

When a Looper workspace is available, read `docs/benchmark-integration.md`, `docs/benchmark-package.md`, and `schemas/benchmark-manifest.schema.json` before changing a package. Inspect an existing package only as a structural example, never as evidence for the new suite. If the workspace files are unavailable, read the bundled `references/benchmark-interface.md` completely and start from the matching file under `templates/`. In that case, mark local schema validation as pending until the package is imported into Looper; do not guess fields or claim validation passed.

## Intake and evidence

Ask for the suite path or repository when it is not available. Establish the intended infrastructure decision and target environments from the user's request. Inspect the suite's versioned source, license, native launcher, inputs, outputs, correctness rules, metrics, task structure, topology, and runtime requirements.

Record the source URL plus a full immutable commit or SHA-256 digest. Determine whether the suite is single-node, client/server, dynamic multi-client/multi-server, distributed accelerator, storage-cluster, or simulator-based. For every machine role, establish the minimum/default/maximum count, OS and architecture, CPU/memory/accelerator/storage/network floor, privileges, placement, and whether its cost belongs in the score. If a material fact cannot be established, leave it unresolved and ask for that fact instead of guessing. Do not describe an unexecuted environment as compatible.

## Build the package

Create or update `benchmarks/<stable-id>/benchmark.yaml`. Keep suite-specific behavior inside the package launcher, container, or normalizer; an ordinary new suite must not add a benchmark-specific branch to the API, scheduler, or Worker.

Use `looper-adapter/v1`. Select the execution model from observed suite behavior. Declare:

- the business category independently from `adapter.executionModel`; never infer one from the other;
- typed parameters and workload/task identities;
- the directed primary metric and every required correctness/SLO check ID;
- `spec.infrastructure` machine groups, count ranges, requirements, placement and links for every multi-machine or hardware-sensitive suite;
- `spec.audit` default repeats, Reference policy, environment axes and required evidence;
- named datasets, artifacts, endpoints, secrets, devices, or topology inputs;
- immutable dependency locks and a machine-enforced placement, network, storage, and environment-evidence policy;
- lifecycle commands, timeouts, and allowed exit codes;
- required native evidence with the `raw-result` role, plus canonical `metrics.jsonl` and `result.json` outputs.

Prefer a fixed-digest container when it is practical. A trusted local-process suite may instead use Looper-managed provisioning when the complete ZIP package is imported and the operator explicitly finalizes registration. Never put secret values in the manifest.

Treat a newly purchased machine as a clean host. Do not require the user to preinstall suite-specific software. Put only the irreducible Worker/host primitives in `runtime.provisioning.hostCapabilities`; list software installed or materialized by the package in `provides`. Add an idempotent `commands.prepare` that verifies pinned downloads, uses `{cache}` for reuse, and fails with an actionable error when the declared network or privilege requirement is unavailable. The full `spec.capabilities` list describes the ready runtime, not what must already exist before selection. Read `references/benchmark-interface.md` for the managed-provisioning fields.

Keep logical roles in `spec.scenario.roles` and physical machine definitions in `spec.infrastructure.nodeGroups`. Use `orchestration: adapter` for currently executable multi-machine suites and require a digest-bound `topology` input; the Adapter owns remote role startup and cleanup. `orchestration: looper` is reserved for future role-level scheduling and must remain Stage 0 when more than one machine is required.

For every executable package, populate `runtime.dependencyLockDigest`, `runtime.dependencies`, and `runtime.executionPolicy` from evidence. Use `network.mode=none` unless the suite demonstrably requires egress. Restricted egress must include an allowlist and byte budget. Device benchmarks must declare a required `device` input and bind it through the experiment; do not encode an operator's host path in the manifest. Require the system-fingerprint fields needed to interpret results. Do not claim that the current Worker supports a policy capability it does not advertise.

If the native suite does not emit Looper observations, add a suite-owned normalizer. Preserve its native output as evidence and make normalization deterministic. A check named in `adapter.requiredChecks` must be emitted in `result.json`; the primary metric must be emitted in `metrics.jsonl` with the declared unit and direction.

## Validate and register

Validate the manifest against the schema and run focused package tests before opening the UI. Start the local Looper API, frontend, and Worker when they are not already healthy.

Use the browser on `/benchmarks/register` to:

1. import the ZIP package containing `benchmark.yaml`, Adapter scripts and required package resources (Stage 0 contract-only entries may still use YAML/JSON);
2. verify the server-derived identity, runtime, topology, primary metric, evidence and audit summary;
3. inspect every automatic constraint and its detail;
4. fix the Package and re-import rather than editing a second copy in the page or bypassing a failed gate;
5. register only when all blocking constraints pass;
6. bind every required input by reference and digest where required;
7. run the smoke action only for a registered executable package whose base host requirements are supported by an online Worker; Looper must deliver the package and run `prepare` automatically.

Browser registration and smoke execution mutate local Looper state and are within scope only when the user asks to configure and register. Ask immediately before any new external upload, untrusted local execution, paid cloud action, or other effect outside local Looper.

## Completion evidence

Do not stop at a rendered form. Report the ZIP path, package digest, manifest digest, registration ID/key, constraint result, tests run, and smoke experiment/attempt outcome when applicable. Clearly distinguish verified facts, expected gate failures, and work still blocked on missing evidence.
