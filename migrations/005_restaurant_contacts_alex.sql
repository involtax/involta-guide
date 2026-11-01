ALTER TABLE restaurants ADD COLUMN phone text NOT NULL DEFAULT '';
ALTER TABLE restaurants ADD COLUMN opening_hours jsonb;
ALTER TABLE restaurants ADD COLUMN alex_star boolean NOT NULL DEFAULT false;
CREATE TABLE alex_sessions (
 id uuid PRIMARY KEY,
 token_hash text NOT NULL UNIQUE,
 expires_at timestamptz NOT NULL,
 revoked boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE reviews ADD COLUMN alex_session_id uuid REFERENCES alex_sessions(id);
CREATE TABLE alex_changes (
 id uuid PRIMARY KEY,
 session_id uuid NOT NULL REFERENCES alex_sessions(id),
 review_id uuid UNIQUE REFERENCES reviews(id),
 location_id uuid REFERENCES locations(id),
 version_id uuid REFERENCES versions(id),
 before_content jsonb,
 after_content jsonb,
 status text NOT NULL,
 detail text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
