"""merge source discovery and benchmark package heads

Revision ID: d2c4e6f8a1b3
Revises: a9d7e5c3b1f0, fa61d43be902
Create Date: 2026-08-23 20:30:00.000000
"""

from collections.abc import Sequence

revision: str = "d2c4e6f8a1b3"
down_revision: str | Sequence[str] | None = ("a9d7e5c3b1f0", "fa61d43be902")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
