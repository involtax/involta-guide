"""Rename catalogue URLs once, preserving identities and cascading log paths."""

import json
from pathlib import Path
from uuid import uuid4

from alembic import op
from sqlalchemy import text

revision = "readable_slugs"
down_revision = "audience_stats"


def upgrade() -> None:
    plan = json.loads(Path(__file__).parents[1].joinpath("data/readable_slugs.json").read_text())
    connection = op.get_bind()
    connection.execute(
        text("LOCK TABLE locations, location_slug_redirects, request_logs IN EXCLUSIVE MODE")
    )
    locations = {
        row["slug"]: row
        for row in connection.execute(text("SELECT id,slug,name,region FROM locations")).mappings()
    }
    aliases = dict(
        connection.execute(text("SELECT slug,location_id FROM location_slug_redirects")).all()
    )
    changes = []
    for item in plan["locations"]:
        current = locations.get(item["old"])
        # A fresh database has no rows yet. A manually renamed location stays as edited.
        if not current or (current["name"], current["region"]) != (item["name"], item["region"]):
            continue
        if item["new"] in locations or item["new"] in aliases:
            raise RuntimeError(f"Переименование остановлено: URL {item['new']} уже занят")
        changes.append({**item, "id": current["id"]})
    for item in plan["redirects"]:
        if item["old"] in aliases and (item["new"] in locations or item["new"] in aliases):
            raise RuntimeError(f"Переименование остановлено: алиас {item['new']} уже занят")

    # All checks precede writes; request_logs has ON UPDATE CASCADE and a generated path.
    for item in changes:
        connection.execute(text("UPDATE locations SET slug=:new WHERE id=:id"), item)
        connection.execute(
            text(
                "INSERT INTO audit(id,action,target,detail) VALUES(:id,'location_slug',:target,:detail)"
            ),
            {
                "id": str(uuid4()),
                "target": str(item["id"]),
                "detail": json.dumps(
                    {"migration": revision, "before": item["old"], "after": item["new"]},
                    ensure_ascii=False,
                ),
            },
        )
    # Existing editorial aliases receive the same spelling; no old URL is kept.
    for item in plan["redirects"]:
        connection.execute(
            text("UPDATE location_slug_redirects SET slug=:new WHERE slug=:old"), item
        )


def downgrade() -> None:
    raise RuntimeError("Restore the matching database backup and application image.")
