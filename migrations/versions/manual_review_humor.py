"""Allow the moderator to keep a review public as humor."""

from alembic import op

revision = "manual_review_humor"
down_revision = "manual_review_category"


def upgrade() -> None:
    op.execute("ALTER TABLE review_analyses DROP CONSTRAINT review_analyses_manual_category_check")
    op.execute(
        "ALTER TABLE review_analyses ADD CONSTRAINT review_analyses_manual_category_check CHECK(manual_category IN ('bugs','humor'))"
    )


def downgrade() -> None:
    op.execute("UPDATE review_analyses SET manual_category=NULL WHERE manual_category='humor'")
    op.execute("ALTER TABLE review_analyses DROP CONSTRAINT review_analyses_manual_category_check")
    op.execute(
        "ALTER TABLE review_analyses ADD CONSTRAINT review_analyses_manual_category_check CHECK(manual_category='bugs')"
    )
