from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from looper_api.models import Base


class SystemOptimizationStudyRecord(Base):
    __tablename__ = "system_optimization_studies"
    __table_args__ = (
        Index("ix_system_optimization_status_updated", "status", "updated_at"),
        Index(
            "ix_system_optimization_baseline_created",
            "baseline_capacity_study_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    baseline_capacity_study_id: Mapped[str] = mapped_column(
        ForeignKey("capacity_studies.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_capacity_study_id: Mapped[str | None] = mapped_column(
        ForeignKey("capacity_studies.id", ondelete="RESTRICT")
    )
    target_id: Mapped[str] = mapped_column(
        ForeignKey("targets.id", ondelete="RESTRICT"), nullable=False
    )
    network: Mapped[str] = mapped_column(String(16), nullable=False)
    minimum_effect: Mapped[float] = mapped_column(Float, nullable=False)
    authorization_profile_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    fencing_token: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    hypothesis_digest: Mapped[str | None] = mapped_column(String(71))
    decision_digest: Mapped[str | None] = mapped_column(String(71))
    snapshot_digest: Mapped[str | None] = mapped_column(String(71))
    rollback_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    orchestration_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    activation_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    problem_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SystemOptimizationArtifactLinkRecord(Base):
    __tablename__ = "system_optimization_artifact_links"
    __table_args__ = (
        UniqueConstraint(
            "study_id",
            "digest",
            "role",
            "name",
            name="uq_system_optimization_artifact_link",
        ),
        Index("ix_system_optimization_artifact_study", "study_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    study_id: Mapped[str] = mapped_column(
        ForeignKey("system_optimization_studies.id", ondelete="CASCADE"), nullable=False
    )
    digest: Mapped[str] = mapped_column(
        ForeignKey("artifacts.digest", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(160), nullable=False)
    producer: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "SystemOptimizationArtifactLinkRecord",
    "SystemOptimizationStudyRecord",
]
