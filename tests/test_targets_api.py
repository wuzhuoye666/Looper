from __future__ import annotations

from looper_api.app import list_targets
from looper_api.config import Settings
from looper_api.models import TargetRecord
from looper_api.seed import seed_system


def test_system_seed_does_not_recreate_local_target(db_session, tmp_path) -> None:
    local = db_session.get(TargetRecord, "local")
    db_session.delete(local)
    db_session.flush()
    seed_system(db_session)
    db_session.flush()

    result = list_targets(
        db_session,
        Settings(data_dir=tmp_path / "looper-data"),
        include_inactive=False,
    )

    assert result["total"] == 0
    assert db_session.get(TargetRecord, "local") is None
