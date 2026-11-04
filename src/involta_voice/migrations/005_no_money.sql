-- Stop monetary writes while preserving historical values for rollback.
ALTER TABLE provider_calls ALTER COLUMN reserved_usd DROP NOT NULL;
CREATE OR REPLACE FUNCTION track_voice_usage() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' AND NEW.kind='analysis' THEN
      INSERT INTO daily_usage(day,calls,cost)
        VALUES((NEW.created_at AT TIME ZONE 'UTC')::date,1,0)
        ON CONFLICT(day) DO UPDATE SET calls=daily_usage.calls+1;
    END IF;
    RETURN NEW;
END $$;
INSERT INTO voice_schema_versions VALUES(5);
