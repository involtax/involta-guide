"""Stable numeric location identities and directed nearby attributes.

Stop all writers first. Guide and the already consolidated Voice schema are
converted in one transaction. Audio names and historical raw provider strings
remain unchanged. The temporary mapping disappears on commit.
"""

import hashlib
import json
import os
import re
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from alembic import op
from sqlalchemy import text

revision = "location_ids"
down_revision = "restaurant_discovery"


def upgrade() -> None:
    conn = op.get_bind()
    quote = conn.dialect.identifier_preparer.quote
    guide = conn.execute(text("SELECT current_schema()")).scalar_one()
    voice = os.environ.get("VOICE_DATABASE_SCHEMA", "voice")
    if voice == guide:
        raise RuntimeError("Guide and Voice must use different schemas")
    guide_tables = set(
        re.findall(
            r"CREATE TABLE (\w+)", (Path(__file__).parents[1] / "current_schema.sql").read_text()
        )
    ) | {"beta_cleanup_files", "beta_transition_report", "voice_inbox"}
    voice_tables = {
        "submissions",
        "runs",
        "stages",
        "outbox",
        "provider_calls",
        "location_epochs",
        "purge_files",
        "intake_windows",
        "admin_events",
        "asr_candidates",
        "asr_run_requests",
    }
    catalog = json.loads((Path(__file__).parents[2] / "data/location-catalog.json").read_text())
    by_slug = {item["slug"]: item["id"] for item in catalog["locations"]}
    by_uuid = {
        str(uuid5(NAMESPACE_URL, "https://involta.ru/guide/" + slug)): number
        for slug, number in by_slug.items()
    }
    conn.exec_driver_sql(
        "CREATE TEMP TABLE location_id_map(old uuid PRIMARY KEY, new integer UNIQUE NOT NULL) ON COMMIT DROP"
    )
    next_id = max(by_slug.values()) + 1
    used = set()
    for old, slug in conn.execute(text("SELECT id::text,slug FROM locations ORDER BY slug")):
        number = by_uuid.get(old, by_slug.get(slug))
        if number is None:
            number, next_id = next_id, next_id + 1
        if number in used:
            raise RuntimeError(
                "Location identity collision; reconcile catalog before migration: " + slug
            )
        used.add(number)
        conn.execute(
            text("INSERT INTO location_id_map VALUES(CAST(:old AS uuid),:new)"),
            {"old": old, "new": number},
        )
    conn.exec_driver_sql("""CREATE FUNCTION pg_temp.location_number(value uuid) RETURNS integer
      LANGUAGE plpgsql AS $$ DECLARE result integer; BEGIN
        IF value IS NULL THEN RETURN NULL; END IF;
        SELECT new INTO STRICT result FROM location_id_map WHERE old=value;
        RETURN result;
      END $$""")
    # Old pair links are deliberately replaced by the versioned JSON lists.
    conn.exec_driver_sql("DROP TABLE location_relations")
    fks = list(
        conn.execute(
            text("""SELECT conrelid::regclass::text,conname,pg_get_constraintdef(oid)
      FROM pg_constraint WHERE contype='f' AND confrelid='locations'::regclass""")
        )
    )
    for table, name, _ in fks:
        conn.exec_driver_sql(f"ALTER TABLE {table} DROP CONSTRAINT {quote(name)}")
    columns = list(
        conn.execute(
            text("""SELECT table_schema,table_name,column_name
      FROM information_schema.columns WHERE udt_name='uuid' AND
      ((table_schema=:guide AND ((table_name='locations' AND column_name IN ('id','parent_id'))
        OR column_name='location_id')) OR (table_schema=:voice AND column_name='location_id'))
      ORDER BY table_schema,table_name,column_name"""),
            {"guide": guide, "voice": voice},
        )
    )
    columns = [
        (schema, table, column)
        for schema, table, column in columns
        if table in (guide_tables if schema == guide else voice_tables)
    ]
    tables = {(schema, table) for schema, table, _ in columns}
    triggers = []
    for schema, table in tables:
        triggers.extend(
            conn.execute(
                text("""SELECT n.nspname,c.relname,t.tgname,pg_get_triggerdef(t.oid)
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE NOT t.tgisinternal AND n.nspname=:schema AND c.relname=:table"""),
                {"schema": schema, "table": table},
            )
        )
    for schema, table, name, _ in triggers:
        conn.exec_driver_sql(f"DROP TRIGGER {quote(name)} ON {quote(schema)}.{quote(table)}")
    conn.exec_driver_sql("ALTER TABLE locations ALTER COLUMN id DROP DEFAULT")
    for schema, table in tables:
        changes = ", ".join(
            f"ALTER COLUMN {quote(column)} TYPE integer USING pg_temp.location_number({quote(column)})"
            for owner, name, column in columns
            if (owner, name) == (schema, table)
        )
        conn.exec_driver_sql(f"ALTER TABLE {quote(schema)}.{quote(table)} {changes}")
    conn.exec_driver_sql(
        "ALTER TABLE locations ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY"
    )
    conn.exec_driver_sql(
        "SELECT setval(pg_get_serial_sequence('locations','id'),greatest(coalesce(max(id),0),"
        + str(max(by_slug.values()))
        + "),true) FROM locations"
    )
    conn.exec_driver_sql(
        "ALTER TABLE locations ADD COLUMN nearby_location_ids integer[] NOT NULL DEFAULT '{}'"
    )
    conn.exec_driver_sql("ALTER TABLE locations ADD CONSTRAINT locations_id_positive CHECK(id>0)")
    conn.exec_driver_sql(
        "ALTER TABLE locations ADD CONSTRAINT locations_nearby_not_self CHECK(NOT id=ANY(nearby_location_ids))"
    )
    # Change structured location references, including frozen Voice contexts and
    # pending outbox/inbox events. Other UUIDs and raw log strings are untouched.
    conn.exec_driver_sql("""CREATE FUNCTION pg_temp.numeric_location_json(value jsonb) RETURNS jsonb
      LANGUAGE plpgsql AS $$ DECLARE result jsonb; pair record; item jsonb; number integer;
      BEGIN
        IF jsonb_typeof(value)='object' THEN
          result='{}'::jsonb;
          FOR pair IN SELECT * FROM jsonb_each(value) LOOP
            item=pg_temp.numeric_location_json(pair.value);
            IF pair.key IN ('location_id','locationId') AND jsonb_typeof(item)='string' THEN
              SELECT new INTO number FROM location_id_map WHERE old::text=item#>>'{}';
              IF FOUND THEN item=to_jsonb(number); END IF;
            ELSIF pair.key='location' AND jsonb_typeof(item)='object' AND jsonb_typeof(item->'id')='string' THEN
              SELECT new INTO number FROM location_id_map WHERE old::text=item->>'id';
              IF FOUND THEN item=jsonb_set(item,'{id}',to_jsonb(number)); END IF;
            END IF;
            result=result||jsonb_build_object(pair.key,item);
          END LOOP;
          RETURN result;
        ELSIF jsonb_typeof(value)='array' THEN
          SELECT coalesce(jsonb_agg(pg_temp.numeric_location_json(e) ORDER BY n),'[]'::jsonb)
            INTO result FROM jsonb_array_elements(value) WITH ORDINALITY a(e,n);
          RETURN result;
        END IF;
        RETURN value;
      END $$""")
    json_columns = list(
        conn.execute(
            text("""SELECT table_schema,table_name,column_name FROM information_schema.columns
      WHERE table_schema IN (:guide,:voice) AND udt_name='jsonb' """).bindparams(
                guide=guide, voice=voice
            )
        )
    )
    for schema, table, column in json_columns:
        if table not in (guide_tables if schema == guide else voice_tables):
            continue
        conn.exec_driver_sql(
            f"UPDATE {quote(schema)}.{quote(table)} SET {quote(column)}=pg_temp.numeric_location_json({quote(column)}) WHERE {quote(column)} IS NOT NULL"
        )
    conn.exec_driver_sql(
        "UPDATE audit a SET target=m.new::text FROM location_id_map m WHERE a.target=m.old::text"
    )
    for identity, detail in conn.execute(text("SELECT id,detail FROM audit")):
        try:
            value = json.loads(detail)
        except (ValueError, TypeError):
            continue
        if isinstance(value, (dict, list)):
            conn.execute(
                text(
                    "UPDATE audit SET detail=pg_temp.numeric_location_json(CAST(:value AS jsonb))::text WHERE id=:id"
                ),
                {"id": identity, "value": detail},
            )
    if (voice, "submissions") in tables:
        for reference, location, epoch, sha, mime, metadata in conn.execute(
            text(
                f"SELECT reference,location_id,content_epoch,sha256,mime,metadata FROM {quote(voice)}.submissions"
            )
        ):
            payload = {
                "sha256": sha,
                "mime": mime,
                "location": location,
                "content_epoch": epoch,
                "language": metadata.get("language", ""),
                "timezone": metadata.get("timezone", ""),
            }
            checksum = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, default=str).encode()
            ).hexdigest()
            conn.execute(
                text(
                    f"UPDATE {quote(voice)}.submissions SET payload_hash=:hash WHERE reference=:id"
                ),
                {"hash": checksum, "id": reference},
            )
        conn.exec_driver_sql(
            f"UPDATE {quote(voice)}.intake_windows w SET key=m.new::text||substring(w.key FROM 37) FROM location_id_map m WHERE left(w.key,36)=m.old::text"
        )
    for item in catalog["locations"]:
        if not item["nearby_location_ids"]:
            continue
        conn.execute(
            text("UPDATE locations SET nearby_location_ids=:nearby WHERE id=:id"),
            {"id": item["id"], "nearby": item["nearby_location_ids"]},
        )
    for table, name, definition in fks:
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD CONSTRAINT {quote(name)} {definition}")
    for _, _, _, definition in triggers:
        conn.exec_driver_sql(definition)
    conn.exec_driver_sql("DELETE FROM settings WHERE key='python_catalog_v1'")


def downgrade() -> None:
    raise RuntimeError("Restore the pre-migration database backup and matching application image.")
