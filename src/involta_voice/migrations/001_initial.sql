CREATE TABLE IF NOT EXISTS voice_schema_versions(version integer PRIMARY KEY);
CREATE TABLE submissions (
 reference uuid PRIMARY KEY, location_id uuid NOT NULL, visitor text NOT NULL,
 idempotency_key uuid NOT NULL, payload_hash text NOT NULL, metadata jsonb NOT NULL,
 audio text NOT NULL UNIQUE, mime text NOT NULL, duration double precision NOT NULL,
 size bigint NOT NULL, sha256 text NOT NULL, repeat_signals jsonb NOT NULL,
 transcript text, active_run integer NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(visitor,idempotency_key)
);
CREATE INDEX submissions_visitor ON submissions(visitor,created_at DESC);
CREATE TABLE runs (
 submission uuid NOT NULL REFERENCES submissions(reference), number integer NOT NULL,
 snapshot jsonb NOT NULL, transcript text, result jsonb, request_key uuid,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(submission,number), UNIQUE(submission,request_key)
);
CREATE TABLE stages (
 submission uuid NOT NULL, run integer NOT NULL, kind text NOT NULL CHECK(kind IN ('transcription','analysis')),
 state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','completed','failed','unknown')),
 attempts integer NOT NULL DEFAULT 0, lease_token uuid, lease_until timestamptz,
 available_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz, finished_at timestamptz,
 request jsonb, raw_response jsonb, error text,
 PRIMARY KEY(submission,run,kind), FOREIGN KEY(submission,run) REFERENCES runs(submission,number)
);
CREATE INDEX stages_queue ON stages(state,available_at);
CREATE TABLE outbox (
 sequence bigserial PRIMARY KEY, event_id uuid NOT NULL UNIQUE,
 submission uuid NOT NULL REFERENCES submissions(reference), run integer NOT NULL,
 kind text NOT NULL, payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(submission,run,kind)
);
CREATE TABLE delivery_ack (consumer text PRIMARY KEY, sequence bigint NOT NULL);
CREATE TABLE voice_settings(key text PRIMARY KEY,value jsonb NOT NULL);
CREATE TABLE provider_calls (
 id uuid PRIMARY KEY, submission uuid NOT NULL, run integer NOT NULL, kind text NOT NULL,
 request jsonb NOT NULL, response jsonb, error text,
 input_tokens bigint,output_tokens bigint,cost_usd double precision,reserved_usd double precision NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE intake_windows(key text PRIMARY KEY, started_at timestamptz NOT NULL DEFAULT now(), count integer NOT NULL);
CREATE TABLE archive_map (source_review uuid PRIMARY KEY, submission uuid NOT NULL UNIQUE REFERENCES submissions(reference),sha256 text NOT NULL);
CREATE TABLE admin_events (id uuid PRIMARY KEY,action text NOT NULL,session_id uuid,
 before_data jsonb NOT NULL,after_data jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
INSERT INTO voice_schema_versions VALUES(1);
