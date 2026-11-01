-- Preserve all existing identities, snapshots, audio/reviews and public URLs.
ALTER TABLE cities RENAME TO locations;
ALTER TABLE restaurants RENAME COLUMN city_id TO location_id;
ALTER TABLE versions RENAME COLUMN city_id TO location_id;
ALTER TABLE reviews RENAME COLUMN city_id TO location_id;
ALTER TABLE proposals RENAME COLUMN city_id TO location_id;
ALTER TABLE jobs RENAME COLUMN city_id TO location_id;
ALTER TABLE locations ALTER COLUMN population DROP NOT NULL;
ALTER TABLE locations ALTER COLUMN population DROP DEFAULT;
ALTER TABLE locations ADD COLUMN kind text NOT NULL DEFAULT 'city'
  CHECK(kind IN ('city','settlement','district','resort','tourist_area','federal_territory','destination'));
ALTER TABLE locations ADD COLUMN parent_id uuid REFERENCES locations(id);
ALTER TABLE locations ADD COLUMN is_hub boolean NOT NULL DEFAULT false;
ALTER TABLE locations ADD COLUMN in_phrase text NOT NULL DEFAULT '';
ALTER TABLE locations ADD COLUMN aliases text[] NOT NULL DEFAULT '{}';
ALTER TABLE locations ADD COLUMN priority text NOT NULL DEFAULT 'medium' CHECK(priority IN ('high','medium','low'));
ALTER TABLE locations ADD COLUMN top_limit smallint NOT NULL DEFAULT 3 CHECK(top_limit BETWEEN 1 AND 10);
ALTER TABLE locations ADD COLUMN population_verified boolean NOT NULL DEFAULT false;
ALTER TABLE locations ADD COLUMN population_basis text NOT NULL DEFAULT 'source_dataset';
ALTER TABLE locations ADD COLUMN source_metadata jsonb NOT NULL DEFAULT '{}';
ALTER TABLE locations ADD COLUMN coverage_note text NOT NULL DEFAULT '';
ALTER TABLE locations ADD COLUMN latitude double precision CHECK(latitude BETWEEN -90 AND 90);
ALTER TABLE locations ADD COLUMN longitude double precision CHECK(longitude BETWEEN -180 AND 180);
ALTER TABLE locations ADD COLUMN bounds jsonb;
ALTER TABLE locations ADD CONSTRAINT locations_not_own_parent CHECK(parent_id IS DISTINCT FROM id);
UPDATE locations SET top_limit=CASE WHEN population>=500000 THEN 5 WHEN population>=100000 THEN 3 ELSE 2 END;
CREATE INDEX locations_parent ON locations(parent_id);
CREATE INDEX locations_kind ON locations(kind,active);
CREATE TABLE location_slug_redirects(slug text PRIMARY KEY,location_id uuid NOT NULL REFERENCES locations(id));
CREATE TABLE location_relations(
  location_id uuid NOT NULL REFERENCES locations(id),
  related_id uuid NOT NULL REFERENCES locations(id),
  PRIMARY KEY(location_id,related_id),CHECK(location_id<>related_id)
);
CREATE TABLE restaurant_locations(
  restaurant_id uuid NOT NULL REFERENCES restaurants(id),
  location_id uuid NOT NULL REFERENCES locations(id),
  PRIMARY KEY(restaurant_id,location_id)
);
INSERT INTO restaurant_locations(restaurant_id,location_id) SELECT id,location_id FROM restaurants;
CREATE INDEX restaurant_locations_location ON restaurant_locations(location_id);
ALTER TABLE restaurants ADD COLUMN latitude double precision CHECK(latitude BETWEEN -90 AND 90);
ALTER TABLE restaurants ADD COLUMN longitude double precision CHECK(longitude BETWEEN -180 AND 180);

CREATE FUNCTION guard_location_tree() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(47031003);
  IF NEW.parent_id IS NOT NULL AND EXISTS(
    WITH RECURSIVE ancestors AS (
      SELECT id,parent_id FROM locations WHERE id=NEW.parent_id
      UNION SELECT p.id,p.parent_id FROM locations p JOIN ancestors a ON p.id=a.parent_id
    ) SELECT 1 FROM ancestors WHERE id=NEW.id
  ) THEN RAISE EXCEPTION 'Нельзя создавать цикл в иерархии локаций'; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER locations_tree_guard BEFORE INSERT OR UPDATE OF parent_id ON locations
  FOR EACH ROW EXECUTE FUNCTION guard_location_tree();

CREATE FUNCTION maintain_location_slug() RETURNS trigger LANGUAGE plpgsql AS $$
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
END $$;
CREATE TRIGGER locations_slug_guard BEFORE INSERT OR UPDATE OF slug ON locations
  FOR EACH ROW EXECUTE FUNCTION maintain_location_slug();
