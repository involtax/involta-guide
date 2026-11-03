-- One explicit direct beta transition. Preserve settings, global sequence and ack.
CREATE TABLE daily_usage(day date PRIMARY KEY,calls bigint NOT NULL,cost double precision NOT NULL);
INSERT INTO daily_usage SELECT (created_at AT TIME ZONE 'UTC')::date,count(*),coalesce(sum(coalesce(cost_usd,reserved_usd)),0)
 FROM provider_calls WHERE kind='analysis' GROUP BY 1;
CREATE FUNCTION track_voice_usage() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
  IF NEW.kind<>'analysis' THEN RETURN NEW; END IF;
  INSERT INTO daily_usage(day,calls,cost)
   VALUES((NEW.created_at AT TIME ZONE 'UTC')::date,CASE WHEN TG_OP='INSERT' THEN 1 ELSE 0 END,
    coalesce(NEW.cost_usd,NEW.reserved_usd)-CASE WHEN TG_OP='UPDATE' THEN coalesce(OLD.cost_usd,OLD.reserved_usd) ELSE 0 END)
  ON CONFLICT(day) DO UPDATE SET calls=daily_usage.calls+excluded.calls,cost=daily_usage.cost+excluded.cost;
  RETURN NEW;
 END $$;
CREATE TRIGGER provider_daily_usage AFTER INSERT OR UPDATE OF cost_usd ON provider_calls
 FOR EACH ROW EXECUTE FUNCTION track_voice_usage();
CREATE TABLE location_epochs(location_id uuid PRIMARY KEY,content_epoch bigint NOT NULL,
 reset_pending boolean NOT NULL DEFAULT false,purged_epoch bigint NOT NULL DEFAULT 0);
CREATE TABLE purge_files(path text PRIMARY KEY,location_id uuid,content_epoch bigint NOT NULL);
INSERT INTO purge_files SELECT audio,location_id,1 FROM submissions;
INSERT INTO location_epochs(location_id,content_epoch) SELECT DISTINCT location_id,1 FROM submissions;
DELETE FROM asr_run_requests; DELETE FROM asr_evaluation_events; DELETE FROM asr_evaluations;
DELETE FROM asr_candidates; DELETE FROM outbox; DELETE FROM provider_calls;
DELETE FROM archive_map; DELETE FROM stages; DELETE FROM runs; DELETE FROM submissions;
DELETE FROM admin_events; DELETE FROM intake_windows;
ALTER TABLE intake_windows ADD COLUMN location_id uuid, ADD COLUMN content_epoch bigint;
DROP TABLE archive_map;
ALTER TABLE submissions DROP COLUMN purpose, ADD COLUMN content_epoch bigint NOT NULL DEFAULT 1;
ALTER TABLE admin_events ADD COLUMN location_id uuid,ADD COLUMN content_epoch bigint;
INSERT INTO voice_schema_versions VALUES(4);
