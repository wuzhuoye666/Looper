from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = b'{"id":"order-1","ok":true}'
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def test_http_capacity_runner_emits_real_closed_accounting(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = tmp_path / "output"
    envelope = {
        "inputs": {
            "capacity-config": {
                "metadata": {
                    "endpoints": {"sut-1": f"http://127.0.0.1:{server.server_port}"},
                    "measurementSeconds": 0.6,
                    "requestTimeoutSeconds": 2,
                    "scenario": {
                        "steps": [
                            {
                                "id": "create-order",
                                "method": "POST",
                                "path": "/orders",
                                "headers": {},
                                "body": {"name": "capacity"},
                                "extract": {"orderId": "id"},
                                "assertions": [
                                    {"kind": "status", "field": "", "expected": 201},
                                    {"kind": "json-equals", "field": "ok", "expected": True},
                                ],
                            }
                        ]
                    },
                }
            }
        },
        "extensions": {
            "targetBinding": {"target_id": "sut-1"},
            "offeredLoad": 10,
        },
        "paths": {},
    }
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    benchmark = Path(__file__).resolve().parents[1] / "benchmarks" / "http-capacity"
    try:
        subprocess.run(
            [
                sys.executable,
                str(benchmark / "runner.py"),
                "--envelope",
                str(envelope_path),
                "--output",
                str(output),
            ],
            check=True,
            timeout=10,
        )
        subprocess.run(
            [
                sys.executable,
                str(benchmark / "normalizer.py"),
                "--envelope",
                str(envelope_path),
                "--output",
                str(output),
            ],
            check=True,
            timeout=10,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    native = json.loads((output / "capacity-native.json").read_text(encoding="utf-8"))
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    metrics = [
        json.loads(line)
        for line in (output / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert native["offeredRequests"] == 6
    assert native["startedRequests"] == 6
    assert native["successRequests"] == 6
    assert native["semanticFailures"] == 0
    assert native["steps"]["create-order"]["samples"] == 6
    assert result["status"] == "succeeded"
    assert result["extensions"]["synthetic"] is False
    assert any(item["metric"] == "committed_tps" and item["value"] > 0 for item in metrics)
