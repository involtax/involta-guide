"""Operational receipt/primary states; no change to historical source data."""

from alembic import op

revision = "review_stream"
down_revision = "filling_top3"


def upgrade() -> None:
    op.execute("ALTER TABLE reviews DROP CONSTRAINT reviews_status_check")
    op.execute(
        "ALTER TABLE reviews ADD CONSTRAINT reviews_status_check CHECK(status IN ('pending','processed','failed','received','transcribed','deleted'))"
    )
    op.execute(
        "CREATE INDEX reviews_city_latest ON reviews(location_id,created_at DESC,number DESC)"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE reviews SET status=CASE WHEN status='transcribed' THEN 'processed' ELSE 'pending' END WHERE status IN ('received','transcribed','deleted')"
    )
    op.execute("DROP INDEX reviews_city_latest")
    op.execute("ALTER TABLE reviews DROP CONSTRAINT reviews_status_check")
    op.execute(
        "ALTER TABLE reviews ADD CONSTRAINT reviews_status_check CHECK(status IN ('pending','processed','failed'))"
    )
