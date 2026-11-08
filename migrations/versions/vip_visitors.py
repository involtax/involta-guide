"""Watched reviews grant the anonymous visitor VIP level one."""

from alembic import op

revision = "vip_visitors"
down_revision = "manual_review_humor"


def upgrade() -> None:
    op.execute("""
    CREATE TABLE vip_visitors (
      visitor_id text PRIMARY KEY,
      level integer NOT NULL CHECK(level > 0)
    );
    ALTER TABLE review_analyses DROP CONSTRAINT review_analyses_manual_category_check;
    ALTER TABLE review_analyses ADD CONSTRAINT review_analyses_manual_category_check
      CHECK(manual_category IN ('bugs','humor','watched'));
    """)


def downgrade() -> None:
    op.execute("""
    UPDATE review_analyses SET manual_category=NULL WHERE manual_category='watched';
    ALTER TABLE review_analyses DROP CONSTRAINT review_analyses_manual_category_check;
    ALTER TABLE review_analyses ADD CONSTRAINT review_analyses_manual_category_check
      CHECK(manual_category IN ('bugs','humor'));
    DROP TABLE vip_visitors;
    """)
