# Looper Benchmark Interface Quick Reference

This bundle is self-contained enough to prepare a package for server validation. `benchmark.yaml` is the contract source of truth; executable Adapter scripts and resources travel beside it in one ZIP. Do not ask the user to re-enter manifest fields or install test software manually.

## Required package facts

- `metadata`: stable ID, name, version, license, immutable source URL plus exactly one full commit or SHA-256 digest.
- `spec.parameters`: typed candidate parameters.
- `spec.workloads`: native task/workload identities and weights.
- `spec.scenario`: decision question, logical roles, primary metric and hard gates when the suite supports infrastructure selection.
- `spec.infrastructure`: physical machine groups and links for multi-machine or hardware-sensitive suites.
- `spec.adapter`: `looper-adapter/v1`, execution model, primary metric, required checks, named inputs and canonical outputs.
- `spec.runtime`: container/process boundary, fixed dependencies, lifecycle argv arrays and execution policy.
- `spec.metrics`: unit, direction, kind, required flag and minimum samples.
- `spec.outputs`: bounded required native evidence.
- `spec.audit`: repeat floor, Reference policy, environment axes and required evidence.

## Infrastructure

Each `nodeGroups` item has:

```yaml
id: server
role: target
count: {minimum: 1, default: 2, maximum: 16}
includedInScore: true
requirements:
  osFamilies: [linux]
  architectures: [x86_64, aarch64]
  capabilities: [container]
  cpu: {minimumLogicalCpus: 16, minimumPhysicalCores: 8, minimumNumaNodes: 1}
  memory: {minimumGiB: 64}
  accelerators:
    - kind: gpu
      vendors: [NVIDIA]
      minimumCount: 8
      minimumMemoryGiBEach: 80
      interconnects: [nvlink]
  storage: {minimumFreeGiB: 500, media: [local-nvme], shared: false, destructive: false}
  network: {minimumGbps: 100, maximumRttMs: 1, fabrics: [roce], rdmaRequired: true}
  privileges: [perf, hugepages]
placement:
  coLocateWith: []
  separateFrom: [client]
  sameZone: true
  dedicated: true
```

Allowed roles: target, load-generator, client, server, database, service, controller, worker, storage, observer, simulator.

`orchestration: adapter` is the current multi-machine mode. The Adapter runs at `primaryNodeGroup`, consumes a required digest-bound `topology` input, verifies every remote node, starts roles, collects evidence and cleans up. `orchestration: looper` with more than one machine is contract-only and must remain `stage0-adapter-only` until Looper supports role-level scheduling.

## Inputs and outputs

Input kinds: dataset, artifact, config, endpoint, secret, device, topology. Secrets use `secret://` references. Host paths do not belong in a portable manifest.

Canonical outputs are always `metrics.jsonl` and `result.json`. Preserve native suite results as required `raw-result`, trace, histogram or profile artifacts. Every `requiredChecks` ID must appear in `result.json`; the primary metric must appear in `metrics.jsonl` with the declared unit.

## Runtime rules

Lifecycle: prepare, warmup, run, normalize, validate, collect, cleanup. Commands are argv arrays. `{cache}` is a persistent directory scoped to the Benchmark version. Production packages use immutable dependencies, an explicit execution boundary, network/storage/placement policy and system-fingerprint required fields.

For automatic deployment to a clean machine, declare:

```yaml
runtime:
  type: local-process
  dependencyLockDigest: sha256:<canonical dependency-lock digest>
  provisioning:
    mode: managed
    hostCapabilities: [linux, python, local-process]
    provides: [suite-cli, unzip]
    cacheKey: sha256:<same canonical dependency-lock digest>
    requiresNetwork: true
    privilege: sudo
  commands:
    prepare:
      argv: ["{python}", "{benchmarkRoot}/prepare.py", "--cache", "{cache}"]
      timeoutSeconds: 900
    run: # ...
```

`prepare.py` must be idempotent, verify dependency digests before extraction, avoid shell strings, and explain missing sudo/network/package-manager support. `hostCapabilities` control whether a clean candidate can be selected. `provides` are installed by Looper after selection and must not be required on the target beforehand.

## Registration

Import the ZIP for an executable package; it must contain exactly one `benchmark.yaml` and keep every file under that manifest directory. Looper validates paths, file count, expanded size and a deterministic package digest, then stores the package without executing it. Finalizing a trusted local-process ZIP is the explicit local installation approval. Stage 0 contract-only entries may use YAML/JSON. Fix failed blocking items in the Package and re-import; the page has no manual contract overrides. Audit reminders do not block catalog registration, but they do block later claims that the Benchmark is admitted for procurement evidence.
