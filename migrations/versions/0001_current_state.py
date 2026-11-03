"""Clean current-state schema; page changes belong to their source logs."""

from pathlib import Path

from alembic import op

revision = "current_state"
down_revision = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        Path(__file__).parents[1].joinpath("current_schema.sql").read_text()
    )


def downgrade() -> None:
    raise RuntimeError(
        "Restore the pre-cutover database backup together with its application image."
    )
