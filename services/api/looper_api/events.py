from __future__ import annotations

from typing import Any

from looper_core.canonical import new_id, utc_now
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from looper_api.models import EventRecord


def append_event(
    session: Session,
    *,
    experiment_id: str | None,
    event_type: str,
    entity_type: str,
    entity_id: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
) -> EventRecord:
    existing = session.scalar(
        select(EventRecord).where(EventRecord.idempotency_key == idempotency_key)
    )
    if existing:
        return existing
    maximum = session.scalar(
        select(func.max(EventRecord.sequence)).where(EventRecord.experiment_id == experiment_id)
    )
    record = EventRecord(
        id=new_id("evt"),
        experiment_id=experiment_id,
        sequence=int(maximum or 0) + 1,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        idempotency_key=idempotency_key,
        payload_json=payload or {},
        created_at=utc_now(),
    )
    session.add(record)
    session.flush()
    return record
