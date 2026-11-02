"""Additive runtime hardening; no existing identities or content are rewritten."""

from alembic import op

revision = "0002_python"
down_revision = "0001_legacy"


def upgrade() -> None:
    op.execute("""
    CREATE TABLE admin_sessions(id uuid PRIMARY KEY,token_hash text NOT NULL UNIQUE,csrf_hash text NOT NULL,expires_at timestamptz NOT NULL,revoked boolean NOT NULL DEFAULT false,created_at timestamptz NOT NULL DEFAULT now());
    ALTER TABLE reviews ADD COLUMN duration_seconds double precision;
    ALTER TABLE reviews ADD COLUMN deleted_at timestamptz;
    ALTER TABLE jobs ADD COLUMN lease_owner uuid;
    ALTER TABLE jobs ADD COLUMN lease_until timestamptz;
    ALTER TABLE jobs ADD COLUMN available_at timestamptz NOT NULL DEFAULT now();
    ALTER TABLE jobs ADD COLUMN applied boolean NOT NULL DEFAULT false;
    CREATE INDEX jobs_lease ON jobs(lease_until) WHERE status='running';
    CREATE INDEX reviews_pending ON reviews(created_at) WHERE status='pending';
    CREATE TABLE ai_usage(id uuid PRIMARY KEY,job_id uuid REFERENCES jobs(id),model text NOT NULL,operation text NOT NULL,status text NOT NULL,input_tokens integer NOT NULL DEFAULT 0,output_tokens integer NOT NULL DEFAULT 0,estimated_cost_usd double precision,detail text NOT NULL DEFAULT '',created_at timestamptz NOT NULL DEFAULT now());
    CREATE INDEX ai_usage_created ON ai_usage(created_at);
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Non-destructive rollback: keep additive tables/columns and roll back application image"
    )
