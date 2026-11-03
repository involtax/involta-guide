ALTER TABLE locations ADD COLUMN page_data jsonb NOT NULL DEFAULT '{"restaurants":[]}'::jsonb;
ALTER TABLE locations ADD COLUMN revision uuid;
ALTER TABLE locations ADD COLUMN updated_at timestamptz;
UPDATE locations l SET page_data=v.content-'intro',revision=gen_random_uuid(),updated_at=v.created_at
FROM versions v WHERE v.id=l.published_version;
UPDATE locations SET page_data=jsonb_build_object('restaurants',draft),revision=gen_random_uuid(),updated_at=now()
WHERE published_version IS NULL AND draft<>'[]'::jsonb;
-- The public page used location.intro and canonical restaurant fields already.
UPDATE locations SET page_data=jsonb_set(page_data,'{restaurants}',
  coalesce((SELECT jsonb_agg(card - ARRAY['name','cuisine','description','mapsUrl','phone','sources','openingHours','alexStar','latitude','longitude','locationId','locationName'] ORDER BY ordinal)
  FROM jsonb_array_elements(page_data->'restaurants') WITH ORDINALITY AS r(card,ordinal)), '[]'::jsonb));
ALTER TABLE locations DROP COLUMN draft, DROP COLUMN published_version, DROP COLUMN draft_version;
ALTER TABLE reviews ADD COLUMN changes jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS admin_session_id uuid REFERENCES admin_sessions(id);
ALTER TABLE jobs ADD COLUMN changes jsonb NOT NULL DEFAULT '[]'::jsonb;
DELETE FROM alex_changes;
ALTER TABLE alex_changes DROP COLUMN version_id, DROP COLUMN before_content, DROP COLUMN after_content;
ALTER TABLE alex_changes ALTER COLUMN session_id DROP NOT NULL;
ALTER TABLE alex_changes ADD COLUMN IF NOT EXISTS admin_session_id uuid REFERENCES admin_sessions(id);
DROP TABLE IF EXISTS voice_candidate_proposals,voice_page_drafts;
DROP TABLE version_reviews;
DROP TABLE versions;
DROP TABLE IF EXISTS schema_migrations;
-- Obsolete source logs can be discarded; live review texts, queues and Voice cursor survive.
DELETE FROM generation_requests;
DELETE FROM ai_usage;
DELETE FROM request_logs WHERE job_id IN (SELECT id FROM jobs WHERE kind='generate');
DELETE FROM jobs WHERE kind='generate';
DELETE FROM audit;
DELETE FROM settings WHERE key='prompt_before_coverage_v2';
