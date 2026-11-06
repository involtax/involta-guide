"""Automatic semantic analysis; explicit approval owns restaurant votes."""

from alembic import op

revision = "review_analysis"
down_revision = "hybrid_search"


def upgrade() -> None:
    op.execute("""
    ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
    ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK(kind IN ('generate','reset','review_analysis'));
    ALTER TABLE jobs ADD COLUMN review_id uuid REFERENCES reviews(id) ON DELETE CASCADE;
    ALTER TABLE reviews ADD COLUMN author_name text, ADD COLUMN edited_text text;
    ALTER TABLE restaurants ADD COLUMN review_score bigint NOT NULL DEFAULT 0;
    CREATE TABLE review_analyses (
      review_id uuid PRIMARY KEY REFERENCES reviews(id) ON DELETE CASCADE,
      job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
      source_text text NOT NULL,
      result jsonb,
      approved_at timestamptz,
      approved_by text
    );
    CREATE TABLE review_restaurant_votes (
      review_id uuid NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
      restaurant_id uuid NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
      value smallint NOT NULL CHECK(value IN (-1,1)),
      PRIMARY KEY(review_id,restaurant_id)
    );
    CREATE FUNCTION apply_review_vote() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE target uuid; delta integer;
    BEGIN
      IF TG_OP='DELETE' THEN target=OLD.restaurant_id; delta=-OLD.value;
      ELSE target=NEW.restaurant_id; delta=NEW.value; END IF;
      UPDATE restaurants r SET review_score=review_score+delta
        WHERE r.id=target AND NOT EXISTS(SELECT 1 FROM locations l WHERE l.id=r.location_id AND l.reset_pending);
      RETURN NULL;
    END $$;
    CREATE TRIGGER review_vote_total AFTER INSERT OR DELETE ON review_restaurant_votes
      FOR EACH ROW EXECUTE FUNCTION apply_review_vote();
    CREATE FUNCTION invalidate_review_analysis() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.status='deleted' OR OLD.transcript IS DISTINCT FROM NEW.transcript THEN
        DELETE FROM review_restaurant_votes WHERE review_id=NEW.id;
        UPDATE review_analyses SET approved_at=NULL,approved_by=NULL WHERE review_id=NEW.id;
        NEW.author_name=NULL; NEW.edited_text=NULL;
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER review_analysis_source BEFORE UPDATE OF transcript,status ON reviews
      FOR EACH ROW EXECUTE FUNCTION invalidate_review_analysis();
    CREATE INDEX jobs_review_analysis ON jobs(review_id,created_at) WHERE kind='review_analysis';
    """)


def downgrade() -> None:
    op.execute("""
    DROP TRIGGER review_analysis_source ON reviews;
    DROP FUNCTION invalidate_review_analysis();
    DROP TABLE review_restaurant_votes;
    DROP FUNCTION apply_review_vote();
    DROP TABLE review_analyses;
    DELETE FROM generation_requests WHERE job_id IN (SELECT id FROM jobs WHERE kind='review_analysis');
    DELETE FROM ai_usage WHERE job_id IN (SELECT id FROM jobs WHERE kind='review_analysis');
    DELETE FROM jobs WHERE kind='review_analysis';
    ALTER TABLE jobs DROP COLUMN review_id;
    ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
    ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK(kind IN ('generate','reset'));
    ALTER TABLE reviews DROP COLUMN author_name,DROP COLUMN edited_text;
    ALTER TABLE restaurants DROP COLUMN review_score;
    """)
