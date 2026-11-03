"""Transcript-first reviews and idempotent Voice integration; additive only."""

from alembic import op

revision = "0006_voice"
down_revision = "0005_current"


def upgrade() -> None:
    op.execute("""
    ALTER TABLE reviews ADD COLUMN admin_session_id uuid REFERENCES admin_sessions(id);
    ALTER TABLE alex_changes ALTER COLUMN session_id DROP NOT NULL;
    ALTER TABLE alex_changes ADD COLUMN admin_session_id uuid REFERENCES admin_sessions(id);
    CREATE TABLE voice_page_drafts (
      location_id uuid PRIMARY KEY REFERENCES locations(id), version_id uuid NOT NULL REFERENCES versions(id),
      base_version uuid REFERENCES versions(id)
    );
    CREATE TABLE voice_candidate_proposals (
      version_id uuid NOT NULL REFERENCES versions(id), proposal_id uuid NOT NULL REFERENCES proposals(id),
      PRIMARY KEY(version_id,proposal_id)
    );
    CREATE TABLE review_texts (
      review_id uuid PRIMARY KEY REFERENCES reviews(id), original_text text NOT NULL,
      editorial_text text NOT NULL, revision integer NOT NULL DEFAULT 1,
      restaurant_id uuid REFERENCES restaurants(id), public boolean NOT NULL DEFAULT false,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    INSERT INTO review_texts(review_id,original_text,editorial_text)
      SELECT id,transcript,transcript FROM reviews
      WHERE transcript IS NOT NULL AND transcript<>'' AND alex_session_id IS NULL;
    CREATE TABLE voice_links (
      review_id uuid PRIMARY KEY REFERENCES reviews(id), reference uuid NOT NULL UNIQUE,
      location_id uuid NOT NULL REFERENCES locations(id), number bigint NOT NULL,
      active_run integer NOT NULL, metadata jsonb NOT NULL, UNIQUE(location_id,number)
    );
    CREATE TABLE voice_results (
      review_id uuid NOT NULL REFERENCES reviews(id), run integer NOT NULL,
      analysis jsonb NOT NULL, PRIMARY KEY(review_id,run)
    );
    CREATE TABLE voice_proposals (
      proposal_id uuid PRIMARY KEY REFERENCES proposals(id), review_id uuid NOT NULL REFERENCES reviews(id),
      run integer NOT NULL, number integer NOT NULL, revision integer NOT NULL DEFAULT 1,
      UNIQUE(review_id,run,number)
    );
    CREATE TABLE voice_fragments (
      review_id uuid NOT NULL REFERENCES reviews(id), run integer NOT NULL,
      number integer NOT NULL, fragment jsonb NOT NULL, PRIMARY KEY(review_id,run,number)
    );
    CREATE TABLE review_fragment_bindings(review_id uuid NOT NULL,run integer NOT NULL,number integer NOT NULL,
      restaurant_id uuid REFERENCES restaurants(id),revision integer NOT NULL DEFAULT 1,
      PRIMARY KEY(review_id,run,number),FOREIGN KEY(review_id,run,number) REFERENCES voice_fragments(review_id,run,number));
    CREATE TABLE voice_inbox (
      event_id uuid PRIMARY KEY, sequence bigint NOT NULL UNIQUE, review_id uuid NOT NULL REFERENCES reviews(id),
      run integer NOT NULL, kind text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE voice_cursor (consumer text PRIMARY KEY, sequence bigint NOT NULL DEFAULT 0);
    INSERT INTO voice_cursor(consumer) VALUES('voice');
    CREATE TABLE review_decisions (
      id uuid PRIMARY KEY,review_id uuid NOT NULL REFERENCES reviews(id),actor text NOT NULL,
      session_id uuid NOT NULL,action text NOT NULL,idempotency_key uuid NOT NULL UNIQUE,
      request_hash text NOT NULL,run integer,before_data jsonb NOT NULL,after_data jsonb NOT NULL,
      expected jsonb NOT NULL,result jsonb NOT NULL,reason text NOT NULL DEFAULT '',
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX voice_review_decisions ON review_decisions(review_id,created_at);
    CREATE TABLE voice_archives(source_review uuid PRIMARY KEY REFERENCES reviews(id),reference uuid NOT NULL UNIQUE,sha256 text NOT NULL);
    """)


def downgrade() -> None:
    raise RuntimeError("Preserve review texts and Voice imports; roll back the application only")
