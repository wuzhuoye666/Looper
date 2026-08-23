from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_migration_graph_has_exactly_one_head() -> None:
    repository = Path(__file__).resolve().parents[1]
    config = Config(str(repository / "alembic.ini"))
    config.set_main_option("script_location", str(repository / "migrations"))

    heads = ScriptDirectory.from_config(config).get_heads()

    assert len(heads) == 1, f"expected one Alembic head, found: {heads}"
