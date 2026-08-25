"""Dependency-free open-arrival HTTP business-iteration load generator."""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import math
import os
import select
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

MANAGED_SSH_TUNNEL_PORT = 18002
DEFAULT_REMOTE_SERVICE_PORT = 8001


def _field(value: Any, path: str) -> Any:
    current = value
    if not path:
        return current
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise KeyError(path)
    return current


def _render(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        rendered = value
        for name, replacement in variables.items():
            rendered = rendered.replace("{{" + name + "}}", str(replacement))
        return rendered
    if isinstance(value, list):
        return [_render(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, variables) for key, item in value.items()}
    return value


def _secret(value: str, secret_root: Path | None) -> str:
    if not value.startswith("secret://"):
        return value
    name = value.removeprefix("secret://")
    if not name or "/" in name or "\\" in name or ".." in name or secret_root is None:
        raise ValueError(f"secret reference {value!r} is unavailable")
    path = (secret_root / name).resolve()
    try:
        path.relative_to(secret_root.resolve())
    except ValueError as error:
        raise ValueError("secret reference escapes the configured secret directory") from error
    return path.read_text(encoding="utf-8").strip()


def _request_step(
    base_url: str,
    step: dict[str, Any],
    variables: dict[str, Any],
    timeout: float,
    secret_root: Path | None,
) -> tuple[bool, bool, float, int, dict[str, Any] | None]:
    path = str(_render(step["path"], variables))
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    headers = {
        str(key): _secret(str(_render(value, variables)), secret_root)
        for key, value in (step.get("headers") or {}).items()
    }
    body = _render(step.get("body"), variables)
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=str(step.get("method") or "GET"),
    )
    started = time.perf_counter()
    try:
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
            status = int(response.status)
            payload = response.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as error:
            status = int(error.code)
            payload = error.read(4 * 1024 * 1024)
        elapsed_ms = (time.perf_counter() - started) * 1000
        document: dict[str, Any] | None = None
        if payload:
            try:
                parsed = json.loads(payload)
                document = parsed if isinstance(parsed, dict) else {"value": parsed}
            except (UnicodeError, json.JSONDecodeError):
                document = None
        status_ok = True
        semantic_ok = True
        for assertion in step.get("assertions") or []:
            kind = assertion.get("kind")
            if kind == "status":
                status_ok = status_ok and status == int(assertion.get("expected", 200))
            elif kind == "json-exists":
                try:
                    _field(document, str(assertion.get("field") or ""))
                except (KeyError, TypeError):
                    semantic_ok = False
            elif kind == "json-equals":
                try:
                    semantic_ok = semantic_ok and _field(
                        document, str(assertion.get("field") or "")
                    ) == assertion.get("expected")
                except (KeyError, TypeError):
                    semantic_ok = False
        if status_ok and semantic_ok and document is not None:
            for name, field in (step.get("extract") or {}).items():
                variables[str(name)] = _field(document, str(field))
        return status_ok, semantic_ok, elapsed_ms, status, document
    except TimeoutError:
        return False, True, (time.perf_counter() - started) * 1000, 0, None


def _iteration(
    base_url: str,
    steps: list[dict[str, Any]],
    timeout: float,
    secret_root: Path | None,
    *,
    attempt_id: str,
    iteration: int,
) -> dict[str, Any]:
    variables: dict[str, Any] = {
        "attempt_id": attempt_id,
        "iteration": iteration,
    }
    started = time.perf_counter()
    details = []
    semantic_ok = True
    try:
        for step in steps:
            status_ok, current_semantic, latency_ms, status, _document = _request_step(
                base_url, step, variables, timeout, secret_root
            )
            details.append(
                {
                    "id": step["id"],
                    "latencyMs": latency_ms,
                    "status": status,
                    "passed": status_ok and current_semantic,
                }
            )
            semantic_ok = semantic_ok and current_semantic
            if status == 0:
                return {
                    "outcome": "timeout",
                    "semanticOk": semantic_ok,
                    "latencyMs": (time.perf_counter() - started) * 1000,
                    "steps": details,
                }
            if not status_ok or not current_semantic:
                return {
                    "outcome": "error",
                    "semanticOk": semantic_ok,
                    "latencyMs": (time.perf_counter() - started) * 1000,
                    "steps": details,
                }
        return {
            "outcome": "success",
            "semanticOk": semantic_ok,
            "latencyMs": (time.perf_counter() - started) * 1000,
            "steps": details,
        }
    except Exception as error:
        return {
            "outcome": "error",
            "semanticOk": False,
            "latencyMs": (time.perf_counter() - started) * 1000,
            "steps": details,
            "error": str(error)[:300],
        }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _requires_managed_ssh_tunnel(base_url: str, target_id: str) -> bool:
    """Recognize the capacity contract's pinned, attempt-scoped SSH endpoint."""

    try:
        parsed = urllib.parse.urlsplit(base_url)
        port = parsed.port
    except ValueError:
        return False
    return (
        target_id.startswith("cloud:")
        and parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost"}
        and port == MANAGED_SSH_TUNNEL_PORT
        and not parsed.username
        and not parsed.password
        and not parsed.path.rstrip("/")
        and not parsed.query
        and not parsed.fragment
    )


class _SshForwardHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        transport = self.server.transport  # type: ignore[attr-defined]
        remote_address = self.server.remote_address  # type: ignore[attr-defined]
        channel = transport.open_channel(
            "direct-tcpip", remote_address, self.request.getpeername()
        )
        if channel is None:
            raise RuntimeError("pinned SSH transport refused the capacity channel")
        try:
            while True:
                ready, _, _ = select.select([self.request, channel], [], [], 5)
                if self.request in ready:
                    data = self.request.recv(65536)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in ready:
                    data = channel.recv(65536)
                    if not data:
                        break
                    self.request.sendall(data)
        finally:
            channel.close()


class _SshForwardServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _credential_data_dir() -> Path:
    configured = os.environ.get("LOOPER_DATA_DIR", "").strip()
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise RuntimeError("managed SSH capacity tunnel requires absolute LOOPER_DATA_DIR")
        return path
    candidates: set[Path] = set()
    for anchor in (Path.cwd().resolve(), Path(__file__).resolve()):
        for parent in (anchor, *anchor.parents):
            data_dir = parent / ".looper"
            if (
                (data_dir / "remote-worker-credentials.key").is_file()
                and (data_dir / "remote-worker-credentials.json").is_file()
            ):
                candidates.add(data_dir)
    if len(candidates) != 1:
        raise RuntimeError(
            "managed SSH capacity tunnel could not identify one encrypted credential store"
        )
    return candidates.pop()


@contextlib.contextmanager
def _attempt_base_url(
    base_url: str,
    target_id: str,
    *,
    remote_service_port: int,
):
    """Yield the direct endpoint or an ephemeral pinned SSH forward.

    The forward is created and destroyed with a single attempt.  Credentials
    are loaded from Looper's encrypted store; none are copied into the
    experiment envelope or benchmark artifacts.
    """

    if not _requires_managed_ssh_tunnel(base_url, target_id):
        yield base_url
        return
    data_dir = _credential_data_dir()
    if not 1 <= remote_service_port <= 65535:
        raise RuntimeError("managed SSH capacity tunnel received an invalid service port")
    try:
        from looper_api.config import Settings
        from looper_api.external_targets import open_ssh_client
        from looper_api.remote_credentials import EncryptedSshCredentialStore
    except ImportError as error:
        raise RuntimeError("managed SSH capacity tunnel dependencies are unavailable") from error

    settings = Settings(data_dir=data_dir)
    request = EncryptedSshCredentialStore(settings).load(target_id)
    client = open_ssh_client(request)
    transport = client.get_transport()
    if transport is None or not transport.is_active():
        client.close()
        raise RuntimeError("pinned SSH capacity transport is not active")
    transport.set_keepalive(15)
    server = _SshForwardServer(("127.0.0.1", 0), _SshForwardHandler)
    server.transport = transport  # type: ignore[attr-defined]
    server.remote_address = ("127.0.0.1", remote_service_port)  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        local_port = int(server.server_address[1])
        yield f"http://127.0.0.1:{local_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelope", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    envelope = json.loads(args.envelope.read_text(encoding="utf-8"))
    config = envelope["inputs"]["capacity-config"]["metadata"]
    target_id = envelope["extensions"]["targetBinding"]["target_id"]
    configured_base_url = config["endpoints"][target_id]
    steps = list(config["scenario"]["steps"])
    offered_rps = float(envelope["extensions"]["offeredLoad"])
    duration = float(config["measurementSeconds"])
    timeout = float(config.get("requestTimeoutSeconds", 10))
    attempt_id = str(envelope.get("attemptId") or "unidentified-attempt")
    secret_value = envelope.get("paths", {}).get("secrets")
    secret_root = Path(secret_value) if secret_value else None
    remote_service_port = int(
        config.get("servicePort") or DEFAULT_REMOTE_SERVICE_PORT
    )
    with _attempt_base_url(
        configured_base_url,
        target_id,
        remote_service_port=remote_service_port,
    ) as base_url:
        return _run_capacity(
            args.output,
            target_id,
            base_url,
            steps,
            offered_rps,
            duration,
            timeout,
            attempt_id,
            secret_root,
        )


def _run_capacity(
    output_dir: Path,
    target_id: str,
    base_url: str,
    steps: list[dict[str, Any]],
    offered_rps: float,
    duration: float,
    timeout: float,
    attempt_id: str,
    secret_root: Path | None,
) -> int:
    offered = max(1, int(offered_rps * duration))
    max_workers = min(512, max(8, int(math.ceil(offered_rps * min(timeout, 2) * 1.5))))
    futures: set[concurrent.futures.Future[dict[str, Any]]] = set()
    results: list[dict[str, Any]] = []
    lagged = 0
    skipped = 0
    peak_pending = 0
    lock = threading.Lock()

    def collect(done: set[concurrent.futures.Future[dict[str, Any]]]) -> None:
        with lock:
            for future in done:
                results.append(future.result())

    started_at = time.perf_counter()
    interval = 1.0 / offered_rps
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        for index in range(offered):
            due = started_at + index * interval
            delay = due - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            lag = time.perf_counter() - due
            if lag > max(interval, 0.01):
                lagged += 1
            done, futures = concurrent.futures.wait(
                futures, timeout=0, return_when=concurrent.futures.FIRST_COMPLETED
            )
            collect(done)
            if len(futures) >= max_workers:
                skipped += 1
                continue
            futures.add(
                executor.submit(
                    _iteration,
                    base_url,
                    steps,
                    timeout,
                    secret_root,
                    attempt_id=attempt_id,
                    iteration=index,
                )
            )
            peak_pending = max(peak_pending, len(futures))
        done, _pending = concurrent.futures.wait(futures)
        collect(done)
    elapsed = max(duration, time.perf_counter() - started_at)
    counts = Counter(str(item["outcome"]) for item in results)
    latencies = [float(item["latencyMs"]) for item in results]
    semantic_failures = sum(item.get("semanticOk") is not True for item in results)
    per_step: dict[str, list[float]] = defaultdict(list)
    per_step_errors: Counter[str] = Counter()
    for item in results:
        for step in item.get("steps") or []:
            per_step[str(step["id"])].append(float(step["latencyMs"]))
            if not step.get("passed"):
                per_step_errors[str(step["id"])] += 1
    started = len(results)
    timeout_count = counts["timeout"]
    completed = started - timeout_count
    success_count = counts["success"]
    output = {
        "schemaVersion": "looper.http-capacity/v1",
        "targetId": target_id,
        "baseUrl": base_url,
        "offeredRps": offered_rps,
        "measurementSeconds": duration,
        "elapsedSeconds": elapsed,
        "offeredRequests": offered,
        "startedRequests": started,
        "completedRequests": completed,
        "timeoutRequests": timeout_count,
        "successRequests": success_count,
        "errorRequests": counts["error"],
        "semanticFailures": semantic_failures,
        "skippedRequests": skipped,
        "rateLimiterLagRatio": lagged / offered,
        "clientHeadroomRatio": max(0.0, 1.0 - peak_pending / max_workers),
        "latency": {
            "samples": len(latencies),
            "p50Ms": _percentile(latencies, 0.50),
            "p95Ms": _percentile(latencies, 0.95),
            "p99Ms": _percentile(latencies, 0.99),
            "p999Ms": _percentile(latencies, 0.999),
            "maxMs": max(latencies, default=0.0),
        },
        "steps": {
            step_id: {
                "samples": len(values),
                "p99Ms": _percentile(values, 0.99),
                "p999Ms": _percentile(values, 0.999),
                "errors": per_step_errors[step_id],
            }
            for step_id, values in per_step.items()
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "capacity-native.json").write_text(
        json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
