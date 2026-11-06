"""Keep manual bug classification independent of AI responses."""

from alembic import op

revision = "manual_review_category"
down_revision = "release_reviews_rural"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE review_analyses ADD COLUMN manual_category text CHECK(manual_category='bugs')"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE review_analyses DROP COLUMN manual_category")
