ALTER TABLE locations ADD COLUMN listed boolean NOT NULL DEFAULT true;
-- An administrative reference is not automatically a public dining destination.
UPDATE locations SET listed=false WHERE region='Москва' AND kind='district';
CREATE FUNCTION guard_location_redirect() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(47031004);
  IF EXISTS(SELECT 1 FROM locations WHERE slug=NEW.slug AND id<>NEW.location_id)
    THEN RAISE EXCEPTION 'URL перенаправления занят другой локацией'; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER location_redirect_guard BEFORE INSERT OR UPDATE ON location_slug_redirects
  FOR EACH ROW EXECUTE FUNCTION guard_location_redirect();
ALTER TABLE locations DROP CONSTRAINT locations_kind_check;
ALTER TABLE locations ADD CONSTRAINT locations_kind_check CHECK(kind IN ('city','settlement','district','neighbourhood','resort','tourist_area','federal_territory','destination'));
