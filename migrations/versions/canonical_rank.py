"""Derive restaurant order and total from the eight criterion scores."""

from alembic import op

revision = "canonical_rank"
down_revision = "beta_core"


def upgrade() -> None:
    op.execute("ALTER TABLE restaurants DROP COLUMN position")
    op.execute("UPDATE restaurants SET ranking=ranking-'totalScore' WHERE ranking ? 'totalScore'")


def downgrade() -> None:
    raise RuntimeError("Restore the pre-ranking Guide backup before starting the previous image.")
