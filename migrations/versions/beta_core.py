"""Direct beta-core transition from the current review_urls schema.

Incompatible beta reviews/logs are deliberately cleared. Geography, admin access,
settings and ordinary owner restaurant cards survive. File references are captured
before deleting rows; `guide beta-cleanup-files --execute` completes their removal.
"""

from alembic import op

revision = "beta_core"
down_revision = "review_urls"


def upgrade() -> None:
    op.execute("""
    CREATE TABLE ai_daily_usage(day date PRIMARY KEY,calls bigint NOT NULL,cost double precision NOT NULL);
    INSERT INTO ai_daily_usage SELECT (created_at AT TIME ZONE 'UTC')::date,count(*),coalesce(sum(estimated_cost_usd),0)
      FROM ai_usage GROUP BY 1;
    CREATE FUNCTION track_beta_ai_usage() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        INSERT INTO ai_daily_usage(day,calls,cost)
        VALUES((NEW.created_at AT TIME ZONE 'UTC')::date,CASE WHEN TG_OP='INSERT' THEN 1 ELSE 0 END,
          coalesce(NEW.estimated_cost_usd,0)-CASE WHEN TG_OP='UPDATE' THEN coalesce(OLD.estimated_cost_usd,0) ELSE 0 END)
        ON CONFLICT(day) DO UPDATE SET calls=ai_daily_usage.calls+excluded.calls,cost=ai_daily_usage.cost+excluded.cost;
        RETURN NEW; END $$;
    CREATE TRIGGER ai_usage_daily AFTER INSERT OR UPDATE OF estimated_cost_usd ON ai_usage
      FOR EACH ROW EXECUTE FUNCTION track_beta_ai_usage();
    ALTER TABLE audit ADD COLUMN location_id uuid REFERENCES locations(id),
      ADD COLUMN content_epoch bigint, ADD COLUMN visitor text;
    CREATE TABLE beta_cleanup_files(path text PRIMARY KEY,location_id uuid REFERENCES locations(id));
    INSERT INTO beta_cleanup_files SELECT DISTINCT ON(audio) audio,location_id FROM reviews
      WHERE audio<>'' AND audio NOT LIKE 'voice:%';
    ALTER TABLE locations ADD COLUMN content_epoch bigint NOT NULL DEFAULT 1 CHECK(content_epoch>0),
      ADD COLUMN initialized_at timestamptz,
      ADD COLUMN reset_pending boolean NOT NULL DEFAULT false,
      ADD COLUMN reset_key uuid, ADD COLUMN reset_error text,
      ADD COLUMN next_log_number bigint NOT NULL DEFAULT 0,
      ADD COLUMN next_review_number bigint NOT NULL DEFAULT 0;
    UPDATE locations l SET next_log_number=coalesce((SELECT max(number) FROM request_logs r WHERE r.location_slug=l.slug),0),
      next_review_number=coalesce((SELECT max(number) FROM voice_links v WHERE v.location_id=l.id),0);
    ALTER TABLE restaurants ADD COLUMN position integer NOT NULL DEFAULT 0,
      ADD COLUMN ranking jsonb;
    UPDATE restaurants r SET position=coalesce((SELECT n::integer FROM locations l,
      jsonb_array_elements(l.page_data->'restaurants') WITH ORDINALITY AS cards(card,n)
      WHERE l.id=r.location_id AND card->>'id'=r.id::text LIMIT 1),0),
      ranking=(SELECT report FROM locations l,jsonb_array_elements(coalesce(l.page_data->'rankingReport','[]')) report
      WHERE l.id=r.location_id AND report->>'name'=r.name
      AND (coalesce(report->>'mapsUrl','')='' OR report->>'mapsUrl'=r.maps_url) LIMIT 1);
    -- The base supplies all direct FK dependencies; never cascade into unrelated tables.
    DELETE FROM review_fragment_bindings; DELETE FROM voice_proposals;
    DELETE FROM voice_fragments; DELETE FROM voice_results; DELETE FROM review_decisions;
    DELETE FROM voice_inbox; DELETE FROM voice_links; DELETE FROM review_texts;
    DELETE FROM proposals; DELETE FROM alex_changes; DELETE FROM voice_archives;
    DELETE FROM generation_requests; DELETE FROM ai_usage; DELETE FROM request_logs;
    DELETE FROM jobs; DELETE FROM reviews;
    DELETE FROM audit WHERE action NOT IN ('admin_login','generation_model','prompt','filling_settings');
    DROP TABLE restaurant_locations,alex_changes,voice_archives;
    ALTER TABLE reviews DROP COLUMN admin_session_id, DROP COLUMN alex_session_id,
      DROP COLUMN deleted_at, ADD COLUMN content_epoch bigint NOT NULL DEFAULT 1;
    DROP TABLE alex_sessions;
    ALTER TABLE reviews RENAME COLUMN ip_hash TO ip;
    ALTER TABLE reviews RENAME COLUMN device TO user_agent;
    ALTER TABLE proposals ADD COLUMN restaurant_id uuid REFERENCES restaurants(id);
    ALTER TABLE voice_fragments ADD COLUMN restaurant_id uuid REFERENCES restaurants(id);
    ALTER TABLE jobs DROP COLUMN review_id, DROP CONSTRAINT jobs_kind_check,
      ADD CONSTRAINT jobs_kind_check CHECK(kind IN ('generate','reset')),
      ADD COLUMN content_epoch bigint NOT NULL DEFAULT 1,
      ADD COLUMN model text NOT NULL DEFAULT '', ADD COLUMN baseline_revision uuid,
      ADD COLUMN public_log boolean NOT NULL DEFAULT true,
      ADD COLUMN report jsonb NOT NULL DEFAULT '{}'::jsonb;
    CREATE UNIQUE INDEX jobs_active_reset ON jobs(location_id) WHERE kind='reset' AND status IN ('queued','running');
    ALTER TABLE voice_inbox ALTER COLUMN review_id DROP NOT NULL;
    ALTER TABLE voice_inbox DROP CONSTRAINT voice_inbox_review_id_fkey,
      ADD COLUMN location_id uuid REFERENCES locations(id),
      ADD COLUMN content_epoch bigint NOT NULL DEFAULT 1,
      ADD COLUMN payload jsonb NOT NULL DEFAULT '{}'::jsonb,
      ADD COLUMN state text NOT NULL DEFAULT 'imported', ADD COLUMN error text;
    CREATE TABLE beta_transition_report(location_id uuid PRIMARY KEY REFERENCES locations(id),
      reason text NOT NULL,restaurants_removed integer NOT NULL);
    INSERT INTO beta_transition_report SELECT l.id,'hub_content_cleared',count(r.id)
      FROM locations l LEFT JOIN restaurants r ON r.location_id=l.id WHERE l.is_hub
      GROUP BY l.id HAVING count(r.id)>0 OR l.intro<>'';
    DELETE FROM restaurants r USING locations l WHERE r.location_id=l.id AND l.is_hub;
    UPDATE locations SET intro='' WHERE is_hub;
    UPDATE locations l SET initialized_at=coalesce(l.updated_at,now())
      WHERE NOT is_hub AND EXISTS(SELECT 1 FROM restaurants r WHERE r.location_id=l.id);
    ALTER TABLE locations DROP COLUMN page_data;
    CREATE OR REPLACE FUNCTION assign_request_log() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE city_slug text; ordinal bigint; BEGIN
        IF NEW.kind<>'generate' OR NEW.location_id IS NULL THEN RETURN NEW; END IF;
        UPDATE locations SET next_log_number=next_log_number+1 WHERE id=NEW.location_id
          RETURNING slug,next_log_number INTO city_slug,ordinal;
        INSERT INTO request_logs(location_slug,number,job_id) VALUES(city_slug,ordinal,NEW.id);
        RETURN NEW; END $$;
    CREATE OR REPLACE FUNCTION guard_beta_restaurant() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        PERFORM 1 FROM locations WHERE id=NEW.location_id FOR SHARE;
        IF EXISTS(SELECT 1 FROM locations WHERE id=NEW.location_id AND (is_hub OR reset_pending))
          THEN RAISE EXCEPTION 'Локация не принимает рестораны'; END IF;
        IF TG_OP='UPDATE' AND OLD.location_id<>NEW.location_id
          THEN RAISE EXCEPTION 'Нельзя переносить ресторан между локациями'; END IF;
        RETURN NEW; END $$;
    CREATE TRIGGER restaurants_owner_guard BEFORE INSERT OR UPDATE ON restaurants
      FOR EACH ROW EXECUTE FUNCTION guard_beta_restaurant();
    CREATE OR REPLACE FUNCTION guard_beta_hub() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NEW.is_hub AND (NEW.initialized_at IS NOT NULL OR EXISTS(SELECT 1 FROM restaurants WHERE location_id=NEW.id))
          THEN RAISE EXCEPTION 'Сначала очистите собственную подборку'; END IF;
        RETURN NEW; END $$;
    CREATE TRIGGER locations_hub_guard BEFORE UPDATE OF is_hub ON locations
      FOR EACH ROW EXECUTE FUNCTION guard_beta_hub();
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Incompatible beta transition: restore the pre-transition database and files with the matching image."
    )
