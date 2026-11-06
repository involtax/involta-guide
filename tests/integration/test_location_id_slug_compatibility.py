"""The numeric cutover must survive URL changes and manual city renames together."""

import json
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import text

from involta_guide.infrastructure.persistence.unit_of_work import create_database

pytestmark = pytest.mark.full_catalog


def test_numeric_identity_survives_catalogue_url_change_and_manual_slug(database, monkeypatch):
    settings, _ = database
    schema = "test_slug_id_" + uuid4().hex
    url = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    monkeypatch.setenv("DATABASE_SCHEMA", schema)
    monkeypatch.setenv("VOICE_DATABASE_SCHEMA", schema + "_voice")
    engine = create_database(settings.model_copy(update={"database_schema": schema}))
    config = Config(str(settings.root_dir / "alembic.ini"))
    original_read = Path.read_text
    catalog_path = settings.root_dir / "data/location-catalog.json"
    catalog = json.loads(catalog_path.read_text())
    legacy_id = uuid5(NAMESPACE_URL, "https://involta.ru/guide/plyos")
    identities = json.loads(
        (settings.root_dir / "migrations/data/location_id_uuids.json").read_text()
    )
    expected = identities[str(legacy_id)]
    city = next(row for row in catalog["locations"] if row["id"] == expected)
    city["slug"] = "ples"

    def changed_catalog(path, *args, **kwargs):
        return (
            json.dumps(catalog, ensure_ascii=False)
            if path == catalog_path
            else original_read(path, *args, **kwargs)
        )

    try:
        command.upgrade(config, "audience_stats")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO locations(id,slug,name,region,intro) VALUES(:id,'my-ples','Плёс','Ивановская область','Сохранённое вступление')"
                ),
                {"id": legacy_id},
            )
        monkeypatch.setattr(Path, "read_text", changed_catalog)
        command.upgrade(config, "head")
        with engine.connect() as conn:
            actual = conn.execute(
                text("SELECT id,slug,intro FROM locations WHERE name='Плёс'")
            ).one()
        assert actual.id == expected
        assert actual.slug == "my-ples" and actual.intro == "Сохранённое вступление"
    finally:
        engine.dispose()
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
