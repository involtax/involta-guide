"""Retire candidate pointers without deleting their historical snapshots."""

from alembic import op

revision = "0007_voice_package"
down_revision = ("0006_voice", "0006_coverage")


def upgrade() -> None:
    op.execute(
        "CREATE TABLE voice_consumer_health (id integer PRIMARY KEY CHECK(id=1), state jsonb NOT NULL)"
    )
    # The old tables remain archival only. Application code never reads/writes them.
    op.execute(
        "COMMENT ON TABLE voice_page_drafts IS 'Retired: retained only for historical audit; not a current page'"
    )


def downgrade() -> None:
    raise RuntimeError("Keep review decisions and snapshots; use a compatible image rollback")
