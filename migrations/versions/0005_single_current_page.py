"""Adopt the last saved page as current; retain all historical snapshots."""

from alembic import op

revision = "0005_current"
down_revision = "0004_background"


def upgrade() -> None:
    op.execute("""
    INSERT INTO audit(id,action,target,detail)
    SELECT gen_random_uuid(),'single_current_adopted',id::text,
      jsonb_build_object('previous_public',published_version,'last_saved',draft_version)::text
    FROM locations WHERE draft_version IS DISTINCT FROM published_version;
    UPDATE locations SET published_version=coalesce(draft_version,published_version),
      draft_version=coalesce(draft_version,published_version);
    UPDATE versions SET published=1 WHERE id IN(SELECT published_version FROM locations);
    """)


def downgrade() -> None:
    raise RuntimeError("Retain current pages and history; roll back the application image only")
