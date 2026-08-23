from __future__ import annotations

from looper_api.app import list_targets
from looper_api.config import Settings


def test_target_catalog_includes_seeded_local_target(db_session, tmp_path) -> None:
    result = list_targets(
        db_session,
        Settings(data_dir=tmp_path / "looper-data"),
        include_inactive=False,
    )

    local = next(item for item in result["items"] if item["id"] == "local")
    assert result["total"] == 1
    assert local["type"] == "local"
    assert local["lifecycleStatus"] == "active"
    assert local["runnable"] is True
