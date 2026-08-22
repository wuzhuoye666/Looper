from __future__ import annotations

import argparse
import os
import platform
import socket
import time
from pathlib import Path

import httpx

from looper_worker.client import ControlPlaneClient
from looper_worker.fingerprint import worker_capabilities, worker_fingerprint
from looper_worker.runner import LocalAttemptRunner, RunnerError, cleanup_orphan_processes


def run_worker(
    api_url: str,
    token: str,
    worker_id: str,
    work_dir: Path,
    once: bool = False,
    secret_dir: Path | None = None,
) -> int:
    client = ControlPlaneClient(api_url, worker_id, token)
    runner = LocalAttemptRunner(client, work_dir, secret_dir)
    cleanup_orphan_processes(work_dir)
    try:
        while True:
            try:
                client.register(
                    name=f"{socket.gethostname()} local worker",
                    capabilities=worker_capabilities(),
                    fingerprint=worker_fingerprint(),
                )
                break
            except httpx.HTTPError as error:
                if once:
                    raise
                print(f"worker registration waiting: {error}", flush=True)
                time.sleep(1)

        while True:
            try:
                claim = client.claim()
                if claim is None:
                    if once:
                        return 0
                    time.sleep(0.75)
                    continue
                print(f"claimed {claim['attemptId']}", flush=True)
                response = runner.run_claim(claim)
                print(f"completed {claim['attemptId']} as {response['status']}", flush=True)
                if once:
                    return 0
            except (httpx.HTTPError, RunnerError, OSError, ValueError) as error:
                print(f"worker error: {error}", flush=True)
                if once:
                    return 1
                time.sleep(1)
    finally:
        client.close()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Run a Looper benchmark worker")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--token", default=os.environ.get("LOOPER_LOCAL_WORKER_TOKEN", "looper-local-development")
    )
    parser.add_argument(
        "--worker-id",
        default=f"local-{platform.node().lower().replace(' ', '-') or 'worker'}",
    )
    parser.add_argument("--work-dir", type=Path, default=Path(".looper/worker"))
    parser.add_argument(
        "--secret-dir",
        type=Path,
        default=(
            Path(os.environ["LOOPER_WORKER_SECRET_DIR"])
            if os.environ.get("LOOPER_WORKER_SECRET_DIR")
            else None
        ),
    )
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    raise SystemExit(
        run_worker(
            arguments.api_url,
            arguments.token,
            arguments.worker_id,
            arguments.work_dir,
            arguments.once,
            arguments.secret_dir,
        )
    )


if __name__ == "__main__":
    cli()
