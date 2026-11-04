import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import text

from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow


def seed(uow: PostgresUow, root: Path) -> int:
    raw = (root / "data/location-catalog.json").read_bytes()
    catalog = json.loads(raw)
    catalog_hash = hashlib.sha256(raw).hexdigest()
    with uow:
        uow.session.execute(text("SELECT pg_advisory_xact_lock(47031002)"))
        if uow.repo.setting("python_catalog_v1") == catalog_hash:
            return 0
        existing = uow.repo.rows("locations", limit=100000)
        by_slug = {row["slug"]: row for row in existing}
        added = 0
        new_ids = set()
        for item in catalog["locations"]:
            if item["slug"] in by_slug:
                continue
            same = next(
                (
                    row
                    for row in existing
                    if row["name"] == item["name"] and row["region"] == item["region"]
                ),
                None,
            )
            if same:
                by_slug[item["slug"]] = same
                continue
            values = {key: value for key, value in item.items() if key != "parent_slug"}
            values["id"] = str(uuid5(NAMESPACE_URL, "https://involta.ru/guide/" + item["slug"]))
            row = uow.repo.add("locations", values)
            by_slug[item["slug"]] = row
            added += 1
            new_ids.add(row["id"])
        # Never overwrite manually edited existing rows, including their parents.
        for item in catalog["locations"]:
            row = by_slug[item["slug"]]
            if row["id"] in new_ids and item["parent_slug"]:
                uow.repo.change(
                    "locations", row["id"], {"parent_id": by_slug[item["parent_slug"]]["id"]}
                )
        for alias in catalog["redirects"]:
            uow.repo.link(
                "location_slug_redirects",
                {"slug": alias["slug"], "location_id": by_slug[alias["location_slug"]]["id"]},
            )
        for relation in catalog["related"]:
            uow.repo.link(
                "location_relations",
                {
                    "location_id": by_slug[relation["location_slug"]]["id"],
                    "related_id": by_slug[relation["related_slug"]]["id"],
                },
            )
        if not uow.repo.setting("prompt"):
            uow.repo.set_setting(
                "prompt", (root / "data/prompts/restaurant-research-v4.txt").read_text()
            )
        uow.repo.set_setting("python_catalog_v1", catalog_hash)
        return added
