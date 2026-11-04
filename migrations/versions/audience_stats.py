"""Thirty days of incremental audience counters; no page or device history."""

from alembic import op

revision = "audience_stats"
down_revision = "review_publication"


def upgrade() -> None:
    op.execute("""CREATE TABLE audience_daily (
      day date NOT NULL,
      visitor_id uuid NOT NULL,
      hits bigint NOT NULL DEFAULT 0 CHECK(hits>=0),
      reviews bigint NOT NULL DEFAULT 0 CHECK(reviews>=0),
      PRIMARY KEY(day,visitor_id)
    )""")


def downgrade() -> None:
    op.execute("DROP TABLE audience_daily")
