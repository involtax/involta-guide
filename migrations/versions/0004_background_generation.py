"""Durable OpenAI background requests; additive for application rollback."""

from alembic import op

revision = "0004_background"
down_revision = "0003_logs"


def upgrade() -> None:
    op.execute("""
    CREATE TABLE generation_requests (
      job_id uuid PRIMARY KEY REFERENCES jobs(id),
      usage_id uuid NOT NULL UNIQUE REFERENCES ai_usage(id),
      response_id text UNIQUE,
      state text NOT NULL DEFAULT 'submitting',
      request_json jsonb NOT NULL,
      response_json jsonb,
      last_error text,
      poll_count integer NOT NULL DEFAULT 0,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      deadline_at timestamptz NOT NULL
    );
    """)


def downgrade() -> None:
    raise RuntimeError("Preserve saved OpenAI responses; roll back the application, not the data")
