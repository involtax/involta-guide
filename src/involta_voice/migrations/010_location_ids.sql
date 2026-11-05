-- Existing UUID rows must be converted atomically with Guide while writers stop.
-- A fresh standalone Voice installation has no location rows and needs no map.
DO $$ DECLARE item record; BEGIN
 FOR item IN SELECT table_name,column_name FROM information_schema.columns
 WHERE table_schema=current_schema() AND column_name='location_id' AND udt_name='uuid' LOOP
  IF EXISTS(SELECT 1 FROM submissions) OR EXISTS(SELECT 1 FROM location_epochs)
    OR EXISTS(SELECT 1 FROM purge_files) OR EXISTS(SELECT 1 FROM admin_events WHERE location_id IS NOT NULL)
    OR EXISTS(SELECT 1 FROM intake_windows WHERE location_id IS NOT NULL) THEN
   RAISE EXCEPTION 'Run Guide location_ids migration on the shared database before Voice migrate';
  END IF;
  EXECUTE format('ALTER TABLE %%I ALTER COLUMN %%I TYPE integer USING NULL::integer',item.table_name,item.column_name);
 END LOOP;
END $$;
INSERT INTO voice_schema_versions(version) VALUES(10);
