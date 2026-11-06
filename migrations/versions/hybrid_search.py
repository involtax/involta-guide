"""Durable search attempts; query/IP storage has no automatic expiration."""

from alembic import op

revision = "hybrid_search"
down_revision = "unbiased_city_scope"


def upgrade() -> None:
    op.execute("""CREATE TABLE search_attempts (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      token uuid NOT NULL UNIQUE,
      query text NOT NULL CHECK(length(query) BETWEEN 1 AND 100),
      ip inet,
      is_admin boolean NOT NULL DEFAULT false,
      created_at timestamptz NOT NULL DEFAULT now(),
      version text NOT NULL,
      server_state text NOT NULL DEFAULT 'not_requested'
        CHECK(server_state IN ('not_requested','ready','error')),
      server_ms double precision,
      server_results jsonb NOT NULL DEFAULT '[]',
      shown jsonb NOT NULL DEFAULT '[]',
      shown_at timestamptz,
      shown_version text,
      shown_complete boolean NOT NULL DEFAULT false,
      cache_hit boolean NOT NULL DEFAULT false,
      client_error boolean NOT NULL DEFAULT false,
      event_seq integer NOT NULL DEFAULT 0,
      end_reason text,
      clicked_id text,
      updated_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX search_attempts_created ON search_attempts(created_at DESC,id DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE search_attempts")
