"""Permanent restaurant numbers; current-state intro edits use the existing audit."""

from alembic import op

revision = "admin_routes"
down_revision = "current_state"


def upgrade() -> None:
    op.execute("ALTER TABLE restaurants ADD COLUMN number BIGINT GENERATED ALWAYS AS IDENTITY")
    op.execute("ALTER TABLE restaurants ADD CONSTRAINT restaurants_number_unique UNIQUE(number)")
    op.execute("ALTER TABLE restaurants ADD CONSTRAINT restaurants_number_positive CHECK(number>0)")
    # request_logs.location_slug cascades; its stored generated path follows the new slug.
    op.execute("""CREATE OR REPLACE FUNCTION maintain_location_slug() RETURNS trigger
    LANGUAGE plpgsql AS $$ BEGIN
      PERFORM pg_advisory_xact_lock(47031004);
      IF EXISTS(SELECT 1 FROM location_slug_redirects WHERE slug=NEW.slug AND location_id<>NEW.id)
        THEN RAISE EXCEPTION 'URL уже используется другой локацией'; END IF;
      DELETE FROM location_slug_redirects WHERE slug=NEW.slug AND location_id=NEW.id;
      RETURN NEW;
    END $$""")


def downgrade() -> None:
    raise RuntimeError("Restore the matching database backup and application image.")
