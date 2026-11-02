from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_guide.infrastructure.seed import seed


def test_migrations_and_geography_are_idempotent(database):
    settings, engine = database
    with PostgresUow(engine) as uow:
        rows = uow.repo.rows("locations", limit=100000)
        ids = {row["id"] for row in rows}
        assert len(rows) == 1448
        assert len([r for r in rows if r["region"] == "Москва" and r["kind"] == "district"]) == 132
        moscow = next(r for r in rows if r["slug"] == "moskva")
        assert len([r for r in rows if r["parent_id"] == moscow["id"] and r["listed"]]) == 20
        assert len([r for r in rows if r["name"] == "Газпром Поляна"]) == 1
        sirius = next(r for r in rows if r["slug"] == "sirius")
        assert sirius["region"] == "Федеральная территория Сириус"
        uow.repo.change("locations", sirius["id"], {"coverage_note": "Ручная правка"})
    command.upgrade(Config(str(settings.root_dir / "alembic.ini")), "head")
    assert seed(PostgresUow(engine), settings.root_dir) == 0
    with PostgresUow(engine) as uow:
        assert {r["id"] for r in uow.repo.rows("locations", limit=100000)} == ids
        assert uow.repo.get("locations", sirius["id"])["coverage_note"] == "Ручная правка"


@pytest.mark.legacy
def test_adopt_real_legacy_ledger_preserves_ids_snapshots_and_foreign_keys(database):
    settings, engine = database
    location_id = restaurant_id = version_id = review_id = None
    with engine.begin() as conn:
        location_id = conn.execute(text("SELECT id FROM locations WHERE slug='ivanovo'")).scalar()
        restaurant_id, version_id, review_id = uuid4(), uuid4(), uuid4()
        conn.execute(
            text(
                "INSERT INTO restaurants(id,location_id,name,cuisine,description,maps_url,phone) VALUES(:id,:location,'Legacy','Русская','Old snapshot','https://yandex.ru/maps/org/test/123/','+74951234567')"
            ),
            {"id": restaurant_id, "location": location_id},
        )
        conn.execute(
            text(
                "INSERT INTO versions(id,location_id,number,content,source,published) VALUES(:id,:location,1,CAST(:content AS jsonb),'legacy',1)"
            ),
            {
                "id": version_id,
                "location": location_id,
                "content": '{"intro":"Старая публикация","restaurants":[]}',
            },
        )
        conn.execute(
            text(
                "UPDATE locations SET published_version=:version,draft_version=:version WHERE id=:id"
            ),
            {"id": location_id, "version": version_id},
        )
        conn.execute(
            text(
                "INSERT INTO reviews(id,location_id,visitor,audio,mime,hash,ip_hash,device) VALUES(:id,:location,'v','legacy.wav','audio/wav','hash','ip','device')"
            ),
            {"id": review_id, "location": location_id},
        )
        conn.execute(
            text("INSERT INTO version_reviews(version_id,review_id) VALUES(:version,:review)"),
            {"version": version_id, "review": review_id},
        )
        before = {
            name: conn.execute(text(f"SELECT count(*) FROM {name}")).scalar()
            for name in [
                "locations",
                "restaurants",
                "versions",
                "reviews",
                "version_reviews",
                "schema_migrations",
            ]
        }
    command.upgrade(Config(str(settings.root_dir / "alembic.ini")), "head")
    with engine.connect() as conn:
        after = {
            name: conn.execute(text(f"SELECT count(*) FROM {name}")).scalar() for name in before
        }
        assert after == before
        assert (
            conn.execute(
                text("SELECT published_version FROM locations WHERE id=:id"), {"id": location_id}
            ).scalar()
            == version_id
        )
        assert (
            conn.execute(
                text("SELECT content FROM versions WHERE id=:id"), {"id": version_id}
            ).scalar()["intro"]
            == "Старая публикация"
        )
        assert (
            conn.execute(
                text("SELECT review_id FROM version_reviews WHERE version_id=:id"),
                {"id": version_id},
            ).scalar()
            == review_id
        )
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_constraint WHERE contype='f' AND connamespace=current_schema()::regnamespace"
                )
            ).scalar()
            >= 20
        )


def test_new_catalog_import_adds_only_new_locations_preserving_manual_changes(database, tmp_path):
    import json

    settings, engine = database
    catalog = json.loads((settings.root_dir / "data/location-catalog.json").read_text())
    root = tmp_path / "import"
    (root / "data").mkdir(parents=True)
    new = {
        **catalog["locations"][0],
        "name": "Новый туристический мыс",
        "slug": "new-tourist-cape",
        "parent_slug": None,
    }
    catalog["locations"].append(new)
    (root / "data/location-catalog.json").write_text(json.dumps(catalog, ensure_ascii=False))
    with PostgresUow(engine) as uow:
        sirius = uow.repo.by_slug("sirius")[0]
        uow.repo.change(
            "locations",
            sirius["id"],
            {"parent_id": None, "coverage_note": "Сохранить ручные данные"},
        )
    assert seed(PostgresUow(engine), root) == 1
    assert seed(PostgresUow(engine), root) == 0
    with PostgresUow(engine) as uow:
        changed = uow.repo.get("locations", sirius["id"])
        assert (
            changed["parent_id"] is None and changed["coverage_note"] == "Сохранить ручные данные"
        )
        assert uow.repo.by_slug("new-tourist-cape")[0]["name"] == new["name"]
