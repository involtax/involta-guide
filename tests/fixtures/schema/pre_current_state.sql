CREATE TABLE alembic_version (
	version_num VARCHAR(32) NOT NULL,
	CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

CREATE TABLE schema_migrations (
	name TEXT NOT NULL,
	checksum TEXT NOT NULL,
	applied_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT schema_migrations_pkey PRIMARY KEY (name)
);

CREATE TABLE versions (
	id UUID NOT NULL,
	location_id UUID NOT NULL,
	number INTEGER NOT NULL,
	content JSONB NOT NULL,
	prompt TEXT,
	model TEXT,
	source TEXT NOT NULL,
	published INTEGER DEFAULT 0 NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT versions_pkey PRIMARY KEY (id),
	CONSTRAINT versions_city_id_id_key UNIQUE NULLS DISTINCT (location_id, id),
	CONSTRAINT versions_city_id_number_key UNIQUE NULLS DISTINCT (location_id, number),
	CONSTRAINT versions_published_check CHECK (published = ANY (ARRAY[0, 1]))
);

CREATE TABLE locations (
	id UUID NOT NULL,
	name TEXT NOT NULL,
	region TEXT NOT NULL,
	slug TEXT NOT NULL,
	population INTEGER,
	active INTEGER DEFAULT 1 NOT NULL,
	intro TEXT DEFAULT ''::text NOT NULL,
	draft JSONB DEFAULT '[]'::jsonb NOT NULL,
	published_version UUID,
	draft_version UUID,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	kind TEXT DEFAULT 'city'::text NOT NULL,
	parent_id UUID,
	is_hub BOOLEAN DEFAULT false NOT NULL,
	in_phrase TEXT DEFAULT ''::text NOT NULL,
	aliases TEXT[] DEFAULT '{}'::text[] NOT NULL,
	priority TEXT DEFAULT 'medium'::text NOT NULL,
	top_limit SMALLINT DEFAULT 3 NOT NULL,
	population_verified BOOLEAN DEFAULT false NOT NULL,
	population_basis TEXT DEFAULT 'source_dataset'::text NOT NULL,
	source_metadata JSONB DEFAULT '{}'::jsonb NOT NULL,
	coverage_note TEXT DEFAULT ''::text NOT NULL,
	latitude DOUBLE PRECISION,
	longitude DOUBLE PRECISION,
	bounds JSONB,
	listed BOOLEAN DEFAULT true NOT NULL,
	CONSTRAINT cities_pkey PRIMARY KEY (id),
	CONSTRAINT cities_slug_key UNIQUE NULLS DISTINCT (slug),
	CONSTRAINT cities_active_check CHECK (active = ANY (ARRAY[0, 1])),
	CONSTRAINT cities_population_check CHECK (population >= 0),
	CONSTRAINT locations_kind_check CHECK (kind = ANY (ARRAY['city'::text, 'settlement'::text, 'district'::text, 'neighbourhood'::text, 'resort'::text, 'tourist_area'::text, 'federal_territory'::text, 'destination'::text])),
	CONSTRAINT locations_latitude_check CHECK (latitude >= '-90'::integer::double precision AND latitude <= 90::double precision),
	CONSTRAINT locations_longitude_check CHECK (longitude >= '-180'::integer::double precision AND longitude <= 180::double precision),
	CONSTRAINT locations_not_own_parent CHECK (parent_id IS DISTINCT FROM id),
	CONSTRAINT locations_priority_check CHECK (priority = ANY (ARRAY['high'::text, 'medium'::text, 'low'::text])),
	CONSTRAINT locations_top_limit_check CHECK (top_limit >= 1 AND top_limit <= 10)
);

CREATE TABLE version_reviews (
	version_id UUID NOT NULL,
	review_id UUID NOT NULL,
	CONSTRAINT version_reviews_pkey PRIMARY KEY (version_id, review_id)
);

CREATE TABLE reviews (
	id UUID NOT NULL,
	location_id UUID NOT NULL,
	visitor TEXT NOT NULL,
	audio TEXT NOT NULL,
	mime TEXT NOT NULL,
	hash TEXT NOT NULL,
	ip_hash TEXT NOT NULL,
	device TEXT NOT NULL,
	transcript TEXT,
	result JSONB,
	status TEXT DEFAULT 'pending'::text NOT NULL,
	moderation TEXT DEFAULT 'pending'::text NOT NULL,
	fraud INTEGER DEFAULT 0 NOT NULL,
	reason TEXT DEFAULT ''::text NOT NULL,
	error TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	alex_session_id UUID,
	duration_seconds DOUBLE PRECISION,
	deleted_at TIMESTAMP WITH TIME ZONE,
	admin_session_id UUID,
	CONSTRAINT reviews_pkey PRIMARY KEY (id),
	CONSTRAINT reviews_fraud_check CHECK (fraud = ANY (ARRAY[0, 1])),
	CONSTRAINT reviews_moderation_check CHECK (moderation = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text])),
	CONSTRAINT reviews_status_check CHECK (status = ANY (ARRAY['pending'::text, 'processed'::text, 'failed'::text]))
);

CREATE TABLE admin_sessions (
	id UUID NOT NULL,
	token_hash TEXT NOT NULL,
	csrf_hash TEXT NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	revoked BOOLEAN DEFAULT false NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT admin_sessions_pkey PRIMARY KEY (id),
	CONSTRAINT admin_sessions_token_hash_key UNIQUE NULLS DISTINCT (token_hash)
);

CREATE TABLE alex_sessions (
	id UUID NOT NULL,
	token_hash TEXT NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	revoked BOOLEAN DEFAULT false NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT alex_sessions_pkey PRIMARY KEY (id),
	CONSTRAINT alex_sessions_token_hash_key UNIQUE NULLS DISTINCT (token_hash)
);

CREATE TABLE proposals (
	id UUID NOT NULL,
	review_id UUID NOT NULL,
	location_id UUID NOT NULL,
	kind TEXT NOT NULL,
	payload JSONB NOT NULL,
	status TEXT DEFAULT 'pending'::text NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT proposals_pkey PRIMARY KEY (id),
	CONSTRAINT proposals_kind_check CHECK (kind = ANY (ARRAY['edit'::text, 'restaurant'::text])),
	CONSTRAINT proposals_status_check CHECK (status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text]))
);

CREATE TABLE settings (
	key TEXT NOT NULL,
	value TEXT NOT NULL,
	CONSTRAINT settings_pkey PRIMARY KEY (key)
);

CREATE TABLE audit (
	id UUID NOT NULL,
	action TEXT NOT NULL,
	target TEXT NOT NULL,
	detail TEXT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT audit_pkey PRIMARY KEY (id)
);

CREATE TABLE login_attempts (
	key TEXT NOT NULL,
	count INTEGER NOT NULL,
	reset_at BIGINT NOT NULL,
	CONSTRAINT login_attempts_pkey PRIMARY KEY (key)
);

CREATE TABLE location_slug_redirects (
	slug TEXT NOT NULL,
	location_id UUID NOT NULL,
	CONSTRAINT location_slug_redirects_pkey PRIMARY KEY (slug)
);

CREATE TABLE location_relations (
	location_id UUID NOT NULL,
	related_id UUID NOT NULL,
	CONSTRAINT location_relations_pkey PRIMARY KEY (location_id, related_id),
	CONSTRAINT location_relations_check CHECK (location_id <> related_id)
);

CREATE TABLE restaurant_locations (
	restaurant_id UUID NOT NULL,
	location_id UUID NOT NULL,
	CONSTRAINT restaurant_locations_pkey PRIMARY KEY (restaurant_id, location_id)
);

CREATE TABLE restaurants (
	id UUID NOT NULL,
	location_id UUID NOT NULL,
	name TEXT NOT NULL,
	cuisine TEXT NOT NULL,
	description TEXT NOT NULL,
	maps_url TEXT NOT NULL,
	sources JSONB DEFAULT '[]'::jsonb NOT NULL,
	active INTEGER DEFAULT 1 NOT NULL,
	latitude DOUBLE PRECISION,
	longitude DOUBLE PRECISION,
	phone TEXT DEFAULT ''::text NOT NULL,
	opening_hours JSONB,
	alex_star BOOLEAN DEFAULT false NOT NULL,
	CONSTRAINT restaurants_pkey PRIMARY KEY (id),
	CONSTRAINT restaurants_active_check CHECK (active = ANY (ARRAY[0, 1])),
	CONSTRAINT restaurants_latitude_check CHECK (latitude >= '-90'::integer::double precision AND latitude <= 90::double precision),
	CONSTRAINT restaurants_longitude_check CHECK (longitude >= '-180'::integer::double precision AND longitude <= 180::double precision)
);

CREATE TABLE jobs (
	id UUID NOT NULL,
	kind TEXT NOT NULL,
	location_id UUID,
	review_id UUID,
	prompt TEXT,
	status TEXT DEFAULT 'queued'::text NOT NULL,
	error TEXT,
	attempts INTEGER DEFAULT 0 NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	lease_owner UUID,
	lease_until TIMESTAMP WITH TIME ZONE,
	available_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	applied BOOLEAN DEFAULT false NOT NULL,
	CONSTRAINT jobs_pkey PRIMARY KEY (id),
	CONSTRAINT jobs_kind_check CHECK (kind = ANY (ARRAY['generate'::text, 'review'::text])),
	CONSTRAINT jobs_status_check CHECK (status = ANY (ARRAY['queued'::text, 'running'::text, 'done'::text, 'failed'::text, 'cancelled'::text]))
);

CREATE TABLE ai_usage (
	id UUID NOT NULL,
	job_id UUID,
	model TEXT NOT NULL,
	operation TEXT NOT NULL,
	status TEXT NOT NULL,
	input_tokens INTEGER DEFAULT 0 NOT NULL,
	output_tokens INTEGER DEFAULT 0 NOT NULL,
	estimated_cost_usd DOUBLE PRECISION,
	detail TEXT DEFAULT ''::text NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT ai_usage_pkey PRIMARY KEY (id)
);

CREATE TABLE request_logs (
	location_slug TEXT NOT NULL,
	number BIGINT NOT NULL,
	job_id UUID NOT NULL,
	path TEXT GENERATED ALWAYS AS (((('/logs/'::text || location_slug) || '/'::text) || (number)::text)) STORED,
	CONSTRAINT request_logs_pkey PRIMARY KEY (location_slug, number),
	CONSTRAINT request_logs_job_id_key UNIQUE NULLS DISTINCT (job_id),
	CONSTRAINT request_logs_path_key UNIQUE NULLS DISTINCT (path),
	CONSTRAINT request_logs_number_check CHECK (number > 0)
);

CREATE TABLE generation_requests (
	job_id UUID NOT NULL,
	usage_id UUID NOT NULL,
	response_id TEXT,
	state TEXT DEFAULT 'submitting'::text NOT NULL,
	request_json JSONB NOT NULL,
	response_json JSONB,
	last_error TEXT,
	poll_count INTEGER DEFAULT 0 NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	deadline_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT generation_requests_pkey PRIMARY KEY (job_id),
	CONSTRAINT generation_requests_response_id_key UNIQUE NULLS DISTINCT (response_id),
	CONSTRAINT generation_requests_usage_id_key UNIQUE NULLS DISTINCT (usage_id)
);

CREATE TABLE alex_changes (
	id UUID NOT NULL,
	session_id UUID,
	review_id UUID,
	location_id UUID,
	version_id UUID,
	before_content JSONB,
	after_content JSONB,
	status TEXT NOT NULL,
	detail TEXT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	admin_session_id UUID,
	CONSTRAINT alex_changes_pkey PRIMARY KEY (id),
	CONSTRAINT alex_changes_review_id_key UNIQUE NULLS DISTINCT (review_id)
);

CREATE TABLE voice_page_drafts (
	location_id UUID NOT NULL,
	version_id UUID NOT NULL,
	base_version UUID,
	CONSTRAINT voice_page_drafts_pkey PRIMARY KEY (location_id)
);

CREATE TABLE voice_candidate_proposals (
	version_id UUID NOT NULL,
	proposal_id UUID NOT NULL,
	CONSTRAINT voice_candidate_proposals_pkey PRIMARY KEY (version_id, proposal_id)
);

CREATE TABLE review_texts (
	review_id UUID NOT NULL,
	original_text TEXT NOT NULL,
	editorial_text TEXT NOT NULL,
	revision INTEGER DEFAULT 1 NOT NULL,
	restaurant_id UUID,
	public BOOLEAN DEFAULT false NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT review_texts_pkey PRIMARY KEY (review_id)
);

CREATE TABLE voice_links (
	review_id UUID NOT NULL,
	reference UUID NOT NULL,
	location_id UUID NOT NULL,
	number BIGINT NOT NULL,
	active_run INTEGER NOT NULL,
	metadata JSONB NOT NULL,
	CONSTRAINT voice_links_pkey PRIMARY KEY (review_id),
	CONSTRAINT voice_links_location_id_number_key UNIQUE NULLS DISTINCT (location_id, number),
	CONSTRAINT voice_links_reference_key UNIQUE NULLS DISTINCT (reference)
);

CREATE TABLE voice_results (
	review_id UUID NOT NULL,
	run INTEGER NOT NULL,
	analysis JSONB NOT NULL,
	CONSTRAINT voice_results_pkey PRIMARY KEY (review_id, run)
);

CREATE TABLE voice_proposals (
	proposal_id UUID NOT NULL,
	review_id UUID NOT NULL,
	run INTEGER NOT NULL,
	number INTEGER NOT NULL,
	revision INTEGER DEFAULT 1 NOT NULL,
	CONSTRAINT voice_proposals_pkey PRIMARY KEY (proposal_id),
	CONSTRAINT voice_proposals_review_id_run_number_key UNIQUE NULLS DISTINCT (review_id, run, number)
);

CREATE TABLE voice_fragments (
	review_id UUID NOT NULL,
	run INTEGER NOT NULL,
	number INTEGER NOT NULL,
	fragment JSONB NOT NULL,
	CONSTRAINT voice_fragments_pkey PRIMARY KEY (review_id, run, number)
);

CREATE TABLE review_fragment_bindings (
	review_id UUID NOT NULL,
	run INTEGER NOT NULL,
	number INTEGER NOT NULL,
	restaurant_id UUID,
	revision INTEGER DEFAULT 1 NOT NULL,
	CONSTRAINT review_fragment_bindings_pkey PRIMARY KEY (review_id, run, number)
);

CREATE TABLE voice_inbox (
	event_id UUID NOT NULL,
	sequence BIGINT NOT NULL,
	review_id UUID NOT NULL,
	run INTEGER NOT NULL,
	kind TEXT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT voice_inbox_pkey PRIMARY KEY (event_id),
	CONSTRAINT voice_inbox_sequence_key UNIQUE NULLS DISTINCT (sequence)
);

CREATE TABLE voice_cursor (
	consumer TEXT NOT NULL,
	sequence BIGINT DEFAULT 0 NOT NULL,
	CONSTRAINT voice_cursor_pkey PRIMARY KEY (consumer)
);

CREATE TABLE review_decisions (
	id UUID NOT NULL,
	review_id UUID NOT NULL,
	actor TEXT NOT NULL,
	session_id UUID NOT NULL,
	action TEXT NOT NULL,
	idempotency_key UUID NOT NULL,
	request_hash TEXT NOT NULL,
	run INTEGER,
	before_data JSONB NOT NULL,
	after_data JSONB NOT NULL,
	expected JSONB NOT NULL,
	result JSONB NOT NULL,
	reason TEXT DEFAULT ''::text NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT review_decisions_pkey PRIMARY KEY (id),
	CONSTRAINT review_decisions_idempotency_key_key UNIQUE NULLS DISTINCT (idempotency_key)
);

CREATE TABLE voice_archives (
	source_review UUID NOT NULL,
	reference UUID NOT NULL,
	sha256 TEXT NOT NULL,
	CONSTRAINT voice_archives_pkey PRIMARY KEY (source_review),
	CONSTRAINT voice_archives_reference_key UNIQUE NULLS DISTINCT (reference)
);

CREATE TABLE voice_consumer_health (
	id INTEGER NOT NULL,
	state JSONB NOT NULL,
	CONSTRAINT voice_consumer_health_pkey PRIMARY KEY (id),
	CONSTRAINT voice_consumer_health_id_check CHECK (id = 1)
);

ALTER TABLE versions ADD CONSTRAINT versions_city_id_fkey FOREIGN KEY(location_id) REFERENCES locations (id);

ALTER TABLE locations ADD CONSTRAINT cities_published_version FOREIGN KEY(id, published_version) REFERENCES versions (location_id, id);

ALTER TABLE locations ADD CONSTRAINT locations_parent_id_fkey FOREIGN KEY(parent_id) REFERENCES locations (id);

ALTER TABLE locations ADD CONSTRAINT cities_draft_version FOREIGN KEY(id, draft_version) REFERENCES versions (location_id, id);

CREATE INDEX locations_parent ON locations (parent_id);

CREATE INDEX locations_kind ON locations (kind, active);

ALTER TABLE version_reviews ADD CONSTRAINT version_reviews_version_id_fkey FOREIGN KEY(version_id) REFERENCES versions (id);

ALTER TABLE version_reviews ADD CONSTRAINT version_reviews_review_id_fkey FOREIGN KEY(review_id) REFERENCES reviews (id);

ALTER TABLE reviews ADD CONSTRAINT reviews_admin_session_id_fkey FOREIGN KEY(admin_session_id) REFERENCES admin_sessions (id);

ALTER TABLE reviews ADD CONSTRAINT reviews_alex_session_id_fkey FOREIGN KEY(alex_session_id) REFERENCES alex_sessions (id);

ALTER TABLE reviews ADD CONSTRAINT reviews_city_id_fkey FOREIGN KEY(location_id) REFERENCES locations (id);

CREATE INDEX reviews_pending ON reviews (created_at) WHERE (status = 'pending'::text);

CREATE INDEX reviews_visitor ON reviews (visitor);

CREATE INDEX reviews_city ON reviews (location_id, created_at DESC);

CREATE INDEX reviews_hash ON reviews (hash);

ALTER TABLE proposals ADD CONSTRAINT proposals_review_id_fkey FOREIGN KEY(review_id) REFERENCES reviews (id);

ALTER TABLE proposals ADD CONSTRAINT proposals_city_id_fkey FOREIGN KEY(location_id) REFERENCES locations (id);

CREATE INDEX proposals_review ON proposals (review_id);

ALTER TABLE location_slug_redirects ADD CONSTRAINT location_slug_redirects_location_id_fkey FOREIGN KEY(location_id) REFERENCES locations (id);

ALTER TABLE location_relations ADD CONSTRAINT location_relations_related_id_fkey FOREIGN KEY(related_id) REFERENCES locations (id);

ALTER TABLE location_relations ADD CONSTRAINT location_relations_location_id_fkey FOREIGN KEY(location_id) REFERENCES locations (id);

ALTER TABLE restaurant_locations ADD CONSTRAINT restaurant_locations_location_id_fkey FOREIGN KEY(location_id) REFERENCES locations (id);

ALTER TABLE restaurant_locations ADD CONSTRAINT restaurant_locations_restaurant_id_fkey FOREIGN KEY(restaurant_id) REFERENCES restaurants (id);

CREATE INDEX restaurant_locations_location ON restaurant_locations (location_id);

ALTER TABLE restaurants ADD CONSTRAINT restaurants_city_id_fkey FOREIGN KEY(location_id) REFERENCES locations (id);

CREATE INDEX restaurants_city ON restaurants (location_id);

ALTER TABLE jobs ADD CONSTRAINT jobs_review_id_fkey FOREIGN KEY(review_id) REFERENCES reviews (id);

ALTER TABLE jobs ADD CONSTRAINT jobs_city_id_fkey FOREIGN KEY(location_id) REFERENCES locations (id);

CREATE UNIQUE INDEX jobs_one_generation ON jobs (location_id) WHERE ((kind = 'generate'::text) AND (status = ANY (ARRAY['queued'::text, 'running'::text])));

CREATE INDEX jobs_queue ON jobs (created_at, id) WHERE (status = 'queued'::text);

CREATE UNIQUE INDEX jobs_one_review ON jobs (review_id) WHERE ((kind = 'review'::text) AND (status = ANY (ARRAY['queued'::text, 'running'::text])));

CREATE INDEX jobs_lease ON jobs (lease_until) WHERE (status = 'running'::text);

ALTER TABLE ai_usage ADD CONSTRAINT ai_usage_job_id_fkey FOREIGN KEY(job_id) REFERENCES jobs (id);

CREATE INDEX ai_usage_created ON ai_usage (created_at);

ALTER TABLE request_logs ADD CONSTRAINT request_logs_job_id_fkey FOREIGN KEY(job_id) REFERENCES jobs (id);

ALTER TABLE request_logs ADD CONSTRAINT request_logs_location_slug_fkey FOREIGN KEY(location_slug) REFERENCES locations (slug) ON UPDATE CASCADE;

ALTER TABLE generation_requests ADD CONSTRAINT generation_requests_usage_id_fkey FOREIGN KEY(usage_id) REFERENCES ai_usage (id);

ALTER TABLE generation_requests ADD CONSTRAINT generation_requests_job_id_fkey FOREIGN KEY(job_id) REFERENCES jobs (id);

ALTER TABLE alex_changes ADD CONSTRAINT alex_changes_session_id_fkey FOREIGN KEY(session_id) REFERENCES alex_sessions (id);

ALTER TABLE alex_changes ADD CONSTRAINT alex_changes_location_id_fkey FOREIGN KEY(location_id) REFERENCES locations (id);

ALTER TABLE alex_changes ADD CONSTRAINT alex_changes_version_id_fkey FOREIGN KEY(version_id) REFERENCES versions (id);

ALTER TABLE alex_changes ADD CONSTRAINT alex_changes_review_id_fkey FOREIGN KEY(review_id) REFERENCES reviews (id);

ALTER TABLE alex_changes ADD CONSTRAINT alex_changes_admin_session_id_fkey FOREIGN KEY(admin_session_id) REFERENCES admin_sessions (id);

ALTER TABLE voice_page_drafts ADD CONSTRAINT voice_page_drafts_version_id_fkey FOREIGN KEY(version_id) REFERENCES versions (id);

ALTER TABLE voice_page_drafts ADD CONSTRAINT voice_page_drafts_location_id_fkey FOREIGN KEY(location_id) REFERENCES locations (id);

ALTER TABLE voice_page_drafts ADD CONSTRAINT voice_page_drafts_base_version_fkey FOREIGN KEY(base_version) REFERENCES versions (id);

ALTER TABLE voice_candidate_proposals ADD CONSTRAINT voice_candidate_proposals_proposal_id_fkey FOREIGN KEY(proposal_id) REFERENCES proposals (id);

ALTER TABLE voice_candidate_proposals ADD CONSTRAINT voice_candidate_proposals_version_id_fkey FOREIGN KEY(version_id) REFERENCES versions (id);

ALTER TABLE review_texts ADD CONSTRAINT review_texts_review_id_fkey FOREIGN KEY(review_id) REFERENCES reviews (id);

ALTER TABLE review_texts ADD CONSTRAINT review_texts_restaurant_id_fkey FOREIGN KEY(restaurant_id) REFERENCES restaurants (id);

ALTER TABLE voice_links ADD CONSTRAINT voice_links_review_id_fkey FOREIGN KEY(review_id) REFERENCES reviews (id);

ALTER TABLE voice_links ADD CONSTRAINT voice_links_location_id_fkey FOREIGN KEY(location_id) REFERENCES locations (id);

ALTER TABLE voice_results ADD CONSTRAINT voice_results_review_id_fkey FOREIGN KEY(review_id) REFERENCES reviews (id);

ALTER TABLE voice_proposals ADD CONSTRAINT voice_proposals_review_id_fkey FOREIGN KEY(review_id) REFERENCES reviews (id);

ALTER TABLE voice_proposals ADD CONSTRAINT voice_proposals_proposal_id_fkey FOREIGN KEY(proposal_id) REFERENCES proposals (id);

ALTER TABLE voice_fragments ADD CONSTRAINT voice_fragments_review_id_fkey FOREIGN KEY(review_id) REFERENCES reviews (id);

ALTER TABLE review_fragment_bindings ADD CONSTRAINT review_fragment_bindings_review_id_run_number_fkey FOREIGN KEY(review_id, run, number) REFERENCES voice_fragments (review_id, run, number);

ALTER TABLE review_fragment_bindings ADD CONSTRAINT review_fragment_bindings_restaurant_id_fkey FOREIGN KEY(restaurant_id) REFERENCES restaurants (id);

ALTER TABLE voice_inbox ADD CONSTRAINT voice_inbox_review_id_fkey FOREIGN KEY(review_id) REFERENCES reviews (id);

ALTER TABLE review_decisions ADD CONSTRAINT review_decisions_review_id_fkey FOREIGN KEY(review_id) REFERENCES reviews (id);

CREATE INDEX voice_review_decisions ON review_decisions (review_id, created_at);

ALTER TABLE voice_archives ADD CONSTRAINT voice_archives_source_review_fkey FOREIGN KEY(source_review) REFERENCES reviews (id);

CREATE OR REPLACE FUNCTION guard_location_tree()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
  PERFORM pg_advisory_xact_lock(47031003);
  IF NEW.parent_id IS NOT NULL AND EXISTS(
    WITH RECURSIVE ancestors AS (
      SELECT id,parent_id FROM locations WHERE id=NEW.parent_id
      UNION SELECT p.id,p.parent_id FROM locations p JOIN ancestors a ON p.id=a.parent_id
    ) SELECT 1 FROM ancestors WHERE id=NEW.id
  ) THEN RAISE EXCEPTION 'Нельзя создавать цикл в иерархии локаций'; END IF;
  RETURN NEW;
END $function$
;

CREATE OR REPLACE FUNCTION maintain_location_slug()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
  PERFORM pg_advisory_xact_lock(47031004);
  IF EXISTS(SELECT 1 FROM location_slug_redirects WHERE slug=NEW.slug AND location_id<>NEW.id)
    THEN RAISE EXCEPTION 'URL уже используется перенаправлением другой локации'; END IF;
  DELETE FROM location_slug_redirects WHERE slug=NEW.slug AND location_id=NEW.id;
  IF TG_OP='UPDATE' AND OLD.slug<>NEW.slug THEN
    INSERT INTO location_slug_redirects(slug,location_id) VALUES(OLD.slug,NEW.id)
      ON CONFLICT(slug) DO UPDATE SET location_id=excluded.location_id;
  END IF;
  RETURN NEW;
END $function$
;

CREATE OR REPLACE FUNCTION guard_location_redirect()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
  PERFORM pg_advisory_xact_lock(47031004);
  IF EXISTS(SELECT 1 FROM locations WHERE slug=NEW.slug AND id<>NEW.location_id)
    THEN RAISE EXCEPTION 'URL перенаправления занят другой локацией'; END IF;
  RETURN NEW;
END $function$
;

CREATE OR REPLACE FUNCTION assign_request_log()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
    DECLARE city_slug text; next_number bigint;
    BEGIN
      IF NEW.location_id IS NULL THEN RETURN NEW; END IF;
      SELECT slug INTO STRICT city_slug FROM locations WHERE id=NEW.location_id FOR NO KEY UPDATE;
      SELECT coalesce(max(number),0)+1 INTO next_number FROM request_logs WHERE location_slug=city_slug;
      INSERT INTO request_logs(location_slug,number,job_id) VALUES(city_slug,next_number,NEW.id);
      RETURN NEW;
    END $function$
;

CREATE TRIGGER location_redirect_guard BEFORE INSERT OR UPDATE ON location_slug_redirects FOR EACH ROW EXECUTE FUNCTION guard_location_redirect();

CREATE TRIGGER locations_slug_guard BEFORE INSERT OR UPDATE OF slug ON locations FOR EACH ROW EXECUTE FUNCTION maintain_location_slug();

CREATE TRIGGER locations_tree_guard BEFORE INSERT OR UPDATE OF parent_id ON locations FOR EACH ROW EXECUTE FUNCTION guard_location_tree();

CREATE TRIGGER jobs_request_log AFTER INSERT ON jobs FOR EACH ROW EXECUTE FUNCTION assign_request_log();
