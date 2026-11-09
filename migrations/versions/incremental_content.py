"""Independent incremental city content; existing catalogue and source history stay intact."""

from alembic import op

revision = "incremental_content"
down_revision = "vip_visitors"


def upgrade() -> None:
    op.execute("""
    ALTER TABLE restaurants ADD COLUMN hidden boolean NOT NULL DEFAULT false;
    ALTER TABLE restaurants ADD COLUMN verification_status text NOT NULL DEFAULT 'needs_verification'
      CHECK(verification_status IN ('verified','needs_verification','closed'));
    UPDATE restaurants SET verification_status='verified' WHERE active=1;
    DROP INDEX jobs_one_generation;
    CREATE UNIQUE INDEX jobs_one_generation ON jobs
      (location_id, (coalesce(report->>'operation','legacy')), (coalesce(report->>'restaurant_id','')))
      WHERE kind='generate' AND status IN ('queued','running');
    """)


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM jobs WHERE kind='generate' AND status IN ('queued','running')
                 GROUP BY location_id HAVING count(*)>1)
      THEN RAISE EXCEPTION 'Drain content jobs before downgrading'; END IF;
    END $$;
    DROP INDEX jobs_one_generation;
    CREATE UNIQUE INDEX jobs_one_generation ON jobs(location_id)
      WHERE kind='generate' AND status IN ('queued','running');
    ALTER TABLE restaurants DROP COLUMN verification_status;
    ALTER TABLE restaurants DROP COLUMN hidden;
    """)
