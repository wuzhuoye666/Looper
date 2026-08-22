from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


class ControlPlaneClient:
    def __init__(self, api_url: str, worker_id: str, token: str) -> None:
        self.base_url = api_url.rstrip("/") + "/api/v1"
        self.worker_id = worker_id
        self.token = token
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"X-Worker-Token": token, "Accept": "application/json"},
            timeout=httpx.Timeout(30, connect=5, read=30, write=120),
        )

    def close(self) -> None:
        self.client.close()

    def register(
        self,
        *,
        name: str,
        capabilities: list[str],
        fingerprint: dict[str, Any],
        max_concurrency: int = 1,
    ) -> dict[str, Any]:
        response = self.client.post(
            "/workers/register",
            json={
                "workerId": self.worker_id,
                "name": name,
                "token": self.token,
                "capabilities": capabilities,
                "fingerprint": fingerprint,
                "maxConcurrency": max_concurrency,
            },
        )
        response.raise_for_status()
        return response.json()

    def claim(self) -> dict[str, Any] | None:
        response = self.client.post("/workers/claim", json={"workerId": self.worker_id})
        response.raise_for_status()
        return response.json().get("claim")

    def start(
        self, attempt_id: str, fencing_token: int, envelope: dict[str, Any]
    ) -> dict[str, Any]:
        response = self.client.post(
            f"/worker-attempts/{attempt_id}/start",
            json={
                "workerId": self.worker_id,
                "fencingToken": fencing_token,
                "envelope": envelope,
            },
        )
        response.raise_for_status()
        return response.json()

    def heartbeat(self, attempt_id: str, fencing_token: int) -> dict[str, Any]:
        response = self.client.post(
            f"/worker-attempts/{attempt_id}/heartbeat",
            json={"workerId": self.worker_id, "fencingToken": fencing_token},
        )
        response.raise_for_status()
        return response.json()

    def upload_artifact(
        self,
        attempt_id: str,
        fencing_token: int,
        path: Path,
        *,
        role: str,
        media_type: str,
        producer: str = "benchmark",
        name: str | None = None,
    ) -> dict[str, Any]:
        with path.open("rb") as stream:
            response = self.client.post(
                f"/worker-attempts/{attempt_id}/artifacts",
                data={
                    "workerId": self.worker_id,
                    "fencingToken": str(fencing_token),
                    "role": role,
                    "name": name or path.name,
                    "mediaType": media_type,
                    "producer": producer,
                },
                files={"file": (name or path.name, stream, media_type)},
            )
        response.raise_for_status()
        return response.json()

    def complete(
        self,
        attempt_id: str,
        fencing_token: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = self.client.post(
            f"/worker-attempts/{attempt_id}/complete",
            json={
                "workerId": self.worker_id,
                "fencingToken": fencing_token,
                **payload,
            },
        )
        response.raise_for_status()
        return response.json()
