"""Persist city/number log identities; retain the original requests and responses."""

from alembic import op

revision = "0003_logs"
down_revision = "0002_python"


def upgrade() -> None:
    op.execute("""
    LOCK TABLE jobs IN SHARE ROW EXCLUSIVE MODE;
    CREATE TABLE request_logs (
      location_slug text NOT NULL REFERENCES locations(slug) ON UPDATE CASCADE,
      number bigint NOT NULL CHECK(number > 0),
      job_id uuid NOT NULL UNIQUE REFERENCES jobs(id),
      path text GENERATED ALWAYS AS ('/logs/' || location_slug || '/' || number::text) STORED,
      PRIMARY KEY(location_slug, number), UNIQUE(path)
    );
    WITH old AS (
      SELECT j.id,l.slug,
        coalesce((a.detail::jsonb->>'number')::bigint,
          row_number() OVER(PARTITION BY j.location_id ORDER BY j.created_at,j.id)) number
      FROM jobs j JOIN locations l ON l.id=j.location_id
      LEFT JOIN LATERAL (SELECT detail FROM audit WHERE action='generation_run' AND target=j.id::text ORDER BY created_at LIMIT 1) a ON true
      WHERE j.kind='generate'
    ) INSERT INTO request_logs(location_slug,number,job_id) SELECT slug,number,id FROM old;
    WITH others AS (
      SELECT j.id,l.slug,
        coalesce((SELECT max(number) FROM request_logs r WHERE r.location_slug=l.slug),0)
        + row_number() OVER(PARTITION BY j.location_id ORDER BY j.created_at,j.id) number
      FROM jobs j JOIN locations l ON l.id=j.location_id WHERE j.kind<>'generate'
    ) INSERT INTO request_logs(location_slug,number,job_id) SELECT slug,number,id FROM others;
    CREATE FUNCTION assign_request_log() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE city_slug text; next_number bigint;
    BEGIN
      IF NEW.location_id IS NULL THEN RETURN NEW; END IF;
      SELECT slug INTO STRICT city_slug FROM locations WHERE id=NEW.location_id FOR NO KEY UPDATE;
      SELECT coalesce(max(number),0)+1 INTO next_number FROM request_logs WHERE location_slug=city_slug;
      INSERT INTO request_logs(location_slug,number,job_id) VALUES(city_slug,next_number,NEW.id);
      RETURN NEW;
    END $$;
    CREATE TRIGGER jobs_request_log AFTER INSERT ON jobs FOR EACH ROW EXECUTE FUNCTION assign_request_log();
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Keep log identities and roll back the application image; destructive downgrade disabled"
    )
