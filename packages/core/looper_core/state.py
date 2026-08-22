from __future__ import annotations

from enum import StrEnum


class ExperimentStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class CandidateStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"


class AttemptStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    UPLOADING = "uploading"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    LOST = "lost"


EXPERIMENT_TRANSITIONS: dict[ExperimentStatus, set[ExperimentStatus]] = {
    ExperimentStatus.DRAFT: {ExperimentStatus.QUEUED, ExperimentStatus.CANCELLED},
    ExperimentStatus.QUEUED: {
        ExperimentStatus.RUNNING,
        ExperimentStatus.PAUSED,
        ExperimentStatus.CANCELLED,
        ExperimentStatus.FAILED,
    },
    ExperimentStatus.RUNNING: {
        ExperimentStatus.PAUSED,
        ExperimentStatus.COMPLETED,
        ExperimentStatus.CANCELLED,
        ExperimentStatus.FAILED,
    },
    ExperimentStatus.PAUSED: {
        ExperimentStatus.QUEUED,
        ExperimentStatus.RUNNING,
        ExperimentStatus.CANCELLED,
        ExperimentStatus.FAILED,
    },
    ExperimentStatus.COMPLETED: set(),
    ExperimentStatus.CANCELLED: set(),
    ExperimentStatus.FAILED: set(),
}

ATTEMPT_TRANSITIONS: dict[AttemptStatus, set[AttemptStatus]] = {
    AttemptStatus.QUEUED: {AttemptStatus.LEASED, AttemptStatus.CANCELLED},
    AttemptStatus.LEASED: {
        AttemptStatus.RUNNING,
        AttemptStatus.LOST,
        AttemptStatus.CANCELLED,
    },
    AttemptStatus.RUNNING: {
        AttemptStatus.UPLOADING,
        AttemptStatus.FAILED,
        AttemptStatus.TIMED_OUT,
        AttemptStatus.CANCELLED,
        AttemptStatus.LOST,
    },
    AttemptStatus.UPLOADING: {
        AttemptStatus.SUCCEEDED,
        AttemptStatus.FAILED,
        AttemptStatus.LOST,
    },
    AttemptStatus.SUCCEEDED: set(),
    AttemptStatus.FAILED: set(),
    AttemptStatus.TIMED_OUT: set(),
    AttemptStatus.CANCELLED: set(),
    AttemptStatus.LOST: set(),
}


class InvalidTransition(ValueError):
    pass


def require_experiment_transition(
    current: ExperimentStatus | str, target: ExperimentStatus | str
) -> None:
    current_status = ExperimentStatus(current)
    target_status = ExperimentStatus(target)
    if target_status not in EXPERIMENT_TRANSITIONS[current_status]:
        raise InvalidTransition(f"experiment cannot transition {current_status} -> {target_status}")


def require_attempt_transition(current: AttemptStatus | str, target: AttemptStatus | str) -> None:
    current_status = AttemptStatus(current)
    target_status = AttemptStatus(target)
    if target_status not in ATTEMPT_TRANSITIONS[current_status]:
        raise InvalidTransition(f"attempt cannot transition {current_status} -> {target_status}")
