from __future__ import annotations

from typing import Any, Literal

from looper_core.contracts import MetricObservation, ResultCheck
from pydantic import BaseModel, ConfigDict, Field


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class WorkerRegister(ProtocolModel):
    worker_id: str = Field(alias="workerId", min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=160)
    token: str = Field(min_length=1)
    capabilities: list[str]
    target_ids: list[str] = Field(default_factory=list, alias="targetIds")
    fingerprint: dict[str, Any]
    max_concurrency: int = Field(default=1, alias="maxConcurrency", ge=1, le=64)


class WorkerClaim(ProtocolModel):
    worker_id: str = Field(alias="workerId")


class AttemptHeartbeat(ProtocolModel):
    worker_id: str = Field(alias="workerId")
    fencing_token: int = Field(alias="fencingToken", ge=1)


class AttemptStart(AttemptHeartbeat):
    envelope: dict[str, Any]


class AttemptCompletion(AttemptHeartbeat):
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=180)
    status: Literal["succeeded", "failed", "timed_out", "cancelled"]
    exit_code: int | None = Field(default=None, alias="exitCode")
    error_message: str | None = Field(default=None, alias="errorMessage", max_length=16000)
    observations: list[MetricObservation] = Field(default_factory=list)
    checks: list[ResultCheck] = Field(default_factory=list)


class ArtifactMetadata(AttemptHeartbeat):
    role: Literal[
        "log", "trace", "result", "raw-result", "profile", "dataset", "histogram", "other"
    ]
    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(alias="mediaType", min_length=1, max_length=160)
    producer: str = Field(default="benchmark", min_length=1, max_length=120)
