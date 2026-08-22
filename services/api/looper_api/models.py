from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from looper_core.state import AttemptStatus, CandidateStatus, ExperimentStatus
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ProjectRecord(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BenchmarkRecord(Base):
    __tablename__ = "benchmarks"
    __table_args__ = (UniqueConstraint("benchmark_id", "version", name="uq_benchmark_version"),)

    key: Mapped[str] = mapped_column(String(180), primary_key=True)
    benchmark_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    license: Mapped[str] = mapped_column(String(80), nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(71), nullable=False, unique=True)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    manifest_path: Mapped[str | None] = mapped_column(Text)
    trusted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BenchmarkRegistrationRecord(Base):
    __tablename__ = "benchmark_registrations"
    __table_args__ = (
        Index("ix_benchmark_registration_status_updated", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    draft_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    constraints_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    manifest_digest: Mapped[str | None] = mapped_column(String(71))
    benchmark_key: Mapped[str | None] = mapped_column(
        ForeignKey("benchmarks.key"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TargetRecord(Base):
    __tablename__ = "targets"
    __table_args__ = (
        Index("ix_target_lifecycle_provider", "lifecycle_status", "provider", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    provider: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    capabilities_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    inventory_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    fingerprint_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    runnable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(
        String(24), default="active", nullable=False
    )
    last_inventory_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inventory_missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inventory_miss_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archive_reason: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CloudCatalogCacheRecord(Base):
    __tablename__ = "cloud_catalog_cache"
    __table_args__ = (
        Index("ix_cloud_catalog_provider_kind", "provider", "resource_type", "expires_at"),
    )

    key: Mapped[str] = mapped_column(String(71), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    region: Mapped[str | None] = mapped_column(String(64))
    zone: Mapped[str | None] = mapped_column(String(64))
    query_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)


class CloudQuoteRecord(Base):
    __tablename__ = "cloud_quotes"
    __table_args__ = (Index("ix_cloud_quote_provider_created", "provider", "created_at"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    spec_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    provider_quote_id: Mapped[str | None] = mapped_column(String(180))
    hourly_amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    estimated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    quote_digest: Mapped[str] = mapped_column(String(71), nullable=False, unique=True)
    provider_details_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CloudOrderRecord(Base):
    __tablename__ = "cloud_orders"
    __table_args__ = (
        UniqueConstraint("quote_id", name="uq_cloud_order_quote"),
        Index("ix_cloud_order_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    quote_id: Mapped[str] = mapped_column(ForeignKey("cloud_quotes.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    client_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    spec_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    quote_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    hourly_amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    confirmation_phrase_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    provider_order_id: Mapped[str | None] = mapped_column(String(180))
    provider_instance_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    provider_response_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExperimentRecord(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=ExperimentStatus.DRAFT, nullable=False)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    spec_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    optimizer_state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CandidateRecord(Base):
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint("experiment_id", "config_digest", name="uq_candidate_config"),
        Index("ix_candidate_experiment_sequence", "experiment_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    config_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=CandidateStatus.PENDING, nullable=False)
    infeasible_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SelectionLoadPointRecord(Base):
    __tablename__ = "selection_load_points"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id", "workload_id", "offered_load_key", name="uq_selection_load_point"
        ),
        UniqueConstraint("experiment_id", "sequence", name="uq_selection_load_point_sequence"),
        Index("ix_selection_load_point_experiment_id", "experiment_id"),
        Index("ix_selection_load_point_status", "experiment_id", "status", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False
    )
    workload_id: Mapped[str] = mapped_column(String(120), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    offered_load: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    offered_load_key: Mapped[str] = mapped_column(String(40), nullable=False)
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    required_repeats: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    analysis_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    analysis_input_digest: Mapped[str | None] = mapped_column(String(71))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvaluationRecord(Base):
    __tablename__ = "evaluations"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id", "workload_id", "target_snapshot_digest", name="uq_evaluation_matrix"
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workload_id: Mapped[str] = mapped_column(String(120), nullable=False)
    target_id: Mapped[str] = mapped_column(ForeignKey("targets.id"), nullable=False)
    target_snapshot_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    target_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=CandidateStatus.PENDING, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AttemptRecord(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("experiment_id", "queue_sequence", name="uq_attempt_queue_sequence"),
        Index("ix_attempt_claim", "status", "queue_sequence", "created_at"),
        Index(
            "ix_attempt_selection_load_point",
            "selection_load_point_id",
            "status",
            "repeat_index",
            "retry_index",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evaluation_id: Mapped[str] = mapped_column(
        ForeignKey("evaluations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    selection_load_point_id: Mapped[str | None] = mapped_column(
        ForeignKey("selection_load_points.id", ondelete="CASCADE"), nullable=True
    )
    repeat_index: Mapped[int] = mapped_column(Integer, nullable=False)
    retry_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    queue_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=AttemptStatus.QUEUED, nullable=False)
    fencing_token: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(100), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    envelope_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    envelope_digest: Mapped[str | None] = mapped_column(String(71))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ObservationRecord(Base):
    __tablename__ = "observations"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id", "metric", "phase", "sample_index", "statistic", name="uq_observation"
        ),
        Index("ix_observation_metric", "attempt_id", "metric"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric: Mapped[str] = mapped_column(String(120), nullable=False)
    value_number: Mapped[float | None] = mapped_column(Float)
    value_boolean: Mapped[bool | None] = mapped_column(Boolean)
    unit: Mapped[str] = mapped_column(String(40), nullable=False)
    phase: Mapped[str] = mapped_column(String(24), nullable=False)
    workload: Mapped[str | None] = mapped_column(String(120))
    sample_index: Mapped[int | None] = mapped_column(Integer)
    sample_count: Mapped[int | None] = mapped_column(Integer)
    statistic: Mapped[str] = mapped_column(String(24), nullable=False)
    timestamp_text: Mapped[str | None] = mapped_column(String(48))
    attributes_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CheckRecord(Base):
    __tablename__ = "checks"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    check_id: Mapped[str] = mapped_column(String(120), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ArtifactRecord(Base):
    __tablename__ = "artifacts"

    digest: Mapped[str] = mapped_column(String(71), primary_key=True)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ArtifactLinkRecord(Base):
    __tablename__ = "artifact_links"
    __table_args__ = (
        UniqueConstraint("attempt_id", "digest", "role", "name", name="uq_artifact_link"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    digest: Mapped[str] = mapped_column(ForeignKey("artifacts.digest"), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(160), nullable=False)
    producer: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AnalysisSnapshotRecord(Base):
    __tablename__ = "analysis_snapshots"
    __table_args__ = (Index("ix_analysis_experiment_created", "experiment_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    policy_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    code_version: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EventRecord(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_event_stream", "experiment_id", "sequence"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    experiment_id: Mapped[str | None] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(180), nullable=False, unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkerRecord(Base):
    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    capabilities_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    fingerprint_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
