CREATE TABLE cities (
  id uuid PRIMARY KEY,
  name text NOT NULL,
  region text NOT NULL,
  slug text NOT NULL UNIQUE,
  population integer NOT NULL DEFAULT 0 CHECK (population >= 0),
  active integer NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  intro text NOT NULL DEFAULT '',
  draft jsonb NOT NULL DEFAULT '[]',
  published_version uuid,
  draft_version uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE restaurants (
  id uuid PRIMARY KEY,
  city_id uuid NOT NULL REFERENCES cities(id),
  name text NOT NULL,
  cuisine text NOT NULL,
  description text NOT NULL,
  maps_url text NOT NULL,
  sources jsonb NOT NULL DEFAULT '[]',
  active integer NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE INDEX restaurants_city ON restaurants(city_id);
CREATE TABLE versions (
  id uuid PRIMARY KEY,
  city_id uuid NOT NULL REFERENCES cities(id),
  number integer NOT NULL,
  content jsonb NOT NULL,
  prompt text,
  model text,
  source text NOT NULL,
  published integer NOT NULL DEFAULT 0 CHECK (published IN (0, 1)),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(city_id, number),
  UNIQUE(city_id, id)
);
ALTER TABLE cities ADD CONSTRAINT cities_published_version FOREIGN KEY(id, published_version) REFERENCES versions(city_id, id);
ALTER TABLE cities ADD CONSTRAINT cities_draft_version FOREIGN KEY(id, draft_version) REFERENCES versions(city_id, id);
CREATE TABLE reviews (
  id uuid PRIMARY KEY,
  city_id uuid NOT NULL REFERENCES cities(id),
  visitor text NOT NULL,
  audio text NOT NULL,
  mime text NOT NULL,
  hash text NOT NULL,
  ip_hash text NOT NULL,
  device text NOT NULL,
  transcript text,
  result jsonb,
  status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processed','failed')),
  moderation text NOT NULL DEFAULT 'pending' CHECK(moderation IN ('pending','approved','rejected')),
  fraud integer NOT NULL DEFAULT 0 CHECK(fraud IN (0,1)),
  reason text NOT NULL DEFAULT '',
  error text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX reviews_visitor ON reviews(visitor);
CREATE INDEX reviews_hash ON reviews(hash);
CREATE INDEX reviews_city ON reviews(city_id, created_at DESC);
CREATE TABLE version_reviews (
  version_id uuid NOT NULL REFERENCES versions(id),
  review_id uuid NOT NULL REFERENCES reviews(id),
  PRIMARY KEY(version_id, review_id)
);
CREATE TABLE proposals (
  id uuid PRIMARY KEY,
  review_id uuid NOT NULL REFERENCES reviews(id),
  city_id uuid NOT NULL REFERENCES cities(id),
  kind text NOT NULL CHECK(kind IN ('edit','restaurant')),
  payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX proposals_review ON proposals(review_id);
CREATE TABLE jobs (
  id uuid PRIMARY KEY,
  kind text NOT NULL CHECK(kind IN ('generate','review')),
  city_id uuid REFERENCES cities(id),
  review_id uuid REFERENCES reviews(id),
  prompt text,
  status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','done','failed','cancelled')),
  error text,
  attempts integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz
);
CREATE INDEX jobs_queue ON jobs(created_at, id) WHERE status='queued';
CREATE UNIQUE INDEX jobs_one_generation ON jobs(city_id) WHERE kind='generate' AND status IN ('queued','running');
CREATE UNIQUE INDEX jobs_one_review ON jobs(review_id) WHERE kind='review' AND status IN ('queued','running');
CREATE TABLE settings(key text PRIMARY KEY, value text NOT NULL);
CREATE TABLE audit(id uuid PRIMARY KEY,action text NOT NULL,target text NOT NULL,detail text NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE login_attempts(key text PRIMARY KEY,count integer NOT NULL,reset_at bigint NOT NULL);
