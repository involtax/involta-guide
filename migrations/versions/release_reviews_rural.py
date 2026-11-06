"""Join the independent review-analysis and rural-discovery migrations."""

revision = "release_reviews_rural"
down_revision = ("review_analysis", "rural_discovery")


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
