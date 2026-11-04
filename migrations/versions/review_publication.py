"""Explicit review publication state; source audio and diagnostics are retained."""

from alembic import op

revision = "review_publication"
down_revision = "review_stream"


def upgrade() -> None:
    op.execute("ALTER TABLE reviews ADD COLUMN publication_status smallint NOT NULL DEFAULT 1")
    op.execute(
        "ALTER TABLE reviews ADD CONSTRAINT reviews_publication_status_check CHECK(publication_status IN (0,1,2))"
    )
    op.execute("UPDATE reviews SET publication_status=0 WHERE status IN ('failed','deleted')")


def downgrade() -> None:
    op.execute("ALTER TABLE reviews DROP COLUMN publication_status")
