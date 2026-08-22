# Local Operations

## Environment and dependency locks

`pnpm setup` runs `scripts/bootstrap.ps1`. With uv available it selects CPython 3.12 from `.python-version`; otherwise it uses the supplied compatible interpreter. `uv.lock` is the resolver source of truth and `requirements.lock` is its exact pip export. Regenerate both together after changing `pyproject.toml`:

```powershell
uv lock
uv export --locked --extra dev --no-emit-project --no-hashes --format requirements-txt --output-file requirements.lock
```

## Security boundary

Looper binds to `127.0.0.1` and accepts only the configured origins and local Host headers. Remote exposure is unsupported in the MVP. The development worker token is suitable only for the loopback development topology.

The local-process runner executes arbitrary benchmark code. Install only trusted manifests locally. It passes a minimal environment, bounds logs and artifacts, rejects path traversal and symlinks, applies per-stage timeouts, terminates process trees, and records cleanup failures. Tencent, Alibaba, Volcengine, and Baidu credentials are never included in run envelopes or inherited by benchmark subprocesses.

Start a Worker with one or more `--target-id` arguments to bind its claim authority. The default is `local`; repeat the flag for an intentionally multi-target Worker. Do not run a wildcard legacy Worker in production. Workers advertise only the execution-policy capabilities they can actually enforce. The bundled Docker runner supports isolated containers with no network and workspace-only storage; restricted egress and bound devices require a dedicated policy-enforcing Worker.

## SQLite

Local mode supports one API process and a local disk. Do not put the database on SMB, NFS, or a synchronized folder. The API configures WAL, foreign keys, full synchronous writes, and a 15-second busy timeout. Back up the SQLite file together with the artifact CAS after a WAL checkpoint.

The API runs `alembic upgrade head` before seeding. A pre-Alembic MVP database is adopted only when its complete table and column inventory matches the known schema; partial or foreign schemas fail startup. For controlled deployments, stop the API, back up the database, and migrate explicitly:

```powershell
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m alembic current
.venv\Scripts\python.exe -m alembic check
```

## Recovery

A worker renews each attempt lease. If a coordinator or worker dies, lease expiry marks the attempt `lost`; a new attempt is appended within the retry budget. Fencing tokens reject late uploads and completion calls from the old lease. A restarted local worker inspects its process identity files and terminates only matching orphaned process trees.

## Evidence

Download an evidence ZIP from `/api/v1/experiments/{id}/evidence`. The bundle contains canonical metadata, events, raw observations, analysis snapshots, artifact links, SHA-256 blobs, and a manifest checksum. Verify offline with:

```powershell
.venv\Scripts\looper.exe evidence verify path\to\bundle.zip
```

## Multi-cloud operations

All four Providers require explicit environment credentials; instance metadata credential discovery is deliberately unavailable. Catalog and quote permissions can be granted without enabling purchase. The API reports missing variable names at `/api/v1/cloud/providers` without returning values.

Real create code exists but is fail-closed by default. Enable it only through the global switch, provider allowlist, independent 32+ character operator token and confirmation secret, exact-price Provider readiness, and spend cap documented in `cloud-market.md`. Enter the operator token through the Web key control; it remains in the current tab session and protects quote/order/search routes with a Bearer header. Confirmation performs a fresh same-spec quote before create.

If an order enters `failed`, obtain a fresh quote instead of retrying it. If it enters `unknown`, do not retry or prepare a replacement until the persisted client token has been reconciled against the provider. An authenticated operator can use the order's manual reconciliation panel to record `submitted` with provider instance IDs or `not_created` with a required note; both outcomes append an audit event. Restart recovery converts interrupted `submitting` rows to `unknown` without making provider calls.
