from alembic import command
from alembic.config import Config

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


def test_new_catalog_import_adds_only_new_locations_preserving_manual_changes(database, tmp_path):
    import json

    settings, engine = database
    catalog = json.loads((settings.root_dir / "data/location-catalog.json").read_text())
    root = tmp_path / "import"
    (root / "data").mkdir(parents=True)
    new = {
        **catalog["locations"][0],
        "id": max(item["id"] for item in catalog["locations"]) + 1,
        "nearby_location_ids": [],
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
