from pathlib import Path

from sqlalchemy import text

from involta_guide.infrastructure.location_catalog import load_catalog
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow


def seed(uow: PostgresUow, root: Path) -> int:
    catalog = load_catalog(root)
    with uow:
        uow.session.execute(text("SELECT pg_advisory_xact_lock(47031002)"))
        existing = uow.repo.rows("locations", limit=100000)
        by_id = {row["id"]: row for row in existing}
        by_slug = {row["slug"]: row for row in existing}
        added = 0
        new_ids = set()
        for item in catalog["locations"]:
            if item["id"] in by_id:
                # ID is permanent: a manually renamed slug must not create another city.
                continue
            if item["slug"] in by_slug:
                raise ValueError("Catalog ID conflicts with existing slug: " + item["slug"])
            values = {key: value for key, value in item.items() if key != "parent_slug"}
            row = uow.repo.add("locations", values)
            by_id[row["id"]] = row
            by_slug[item["slug"]] = row
            added += 1
            new_ids.add(row["id"])
        catalog_by_slug = {item["slug"]: item["id"] for item in catalog["locations"]}
        for item in catalog["locations"]:
            if item["id"] in new_ids and item["parent_slug"]:
                uow.repo.change(
                    "locations", item["id"], {"parent_id": catalog_by_slug[item["parent_slug"]]}
                )
            # Git owns the lists at import time; never infer reverse or transitive links.
            if by_id[item["id"]]["nearby_location_ids"] != item["nearby_location_ids"]:
                uow.repo.change(
                    "locations", item["id"], {"nearby_location_ids": item["nearby_location_ids"]}
                )
        uow.session.execute(
            text("""SELECT setval(pg_get_serial_sequence('locations','id'),
            greatest(coalesce(max(id),1), (SELECT last_value FROM locations_id_seq)),true)
            FROM locations""")
        )
        for alias in catalog["redirects"]:
            uow.repo.link(
                "location_slug_redirects",
                {"slug": alias["slug"], "location_id": catalog_by_slug[alias["location_slug"]]},
            )
        if not uow.repo.setting("prompt"):
            uow.repo.set_setting(
                "prompt", (root / "data/prompts/restaurant-research-v4.txt").read_text()
            )
        return added
