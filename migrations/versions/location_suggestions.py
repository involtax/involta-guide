"""Indexed typo fallback over the existing catalogue; no search copy."""

from alembic import op

revision = "location_suggestions"
down_revision = "audience_stats"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
    op.execute("""CREATE INDEX locations_name_trgm ON locations
      USING gin (translate(lower(name),'ё','е') public.gin_trgm_ops)""")


def downgrade() -> None:
    op.execute("DROP INDEX locations_name_trgm")
    # The public extension is shared database infrastructure, not Guide-owned data.
