"""Versioned geographic identities and independent nearby lists."""

import json
from pathlib import Path
from typing import Any


def load_catalog(root: Path) -> dict[str, Any]:
    catalog = json.loads((root / "data/location-catalog.json").read_text())
    locations = catalog["locations"]
    ids = [item["id"] for item in locations]
    slugs = [item["slug"] for item in locations]
    if any(type(value) is not int or value < 1 for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("Catalog requires unique positive integer IDs")
    if len(set(slugs)) != len(slugs):
        raise ValueError("Catalog requires unique slugs")
    known = set(ids)
    for item in locations:
        nearby = item["nearby_location_ids"]
        if (
            not isinstance(nearby, list)
            or any(type(value) is not int or value not in known for value in nearby)
            or len(set(nearby)) != len(nearby)
            or item["id"] in nearby
        ):
            raise ValueError("Invalid nearby IDs for " + item["slug"])
        if item["parent_slug"] and item["parent_slug"] not in slugs:
            raise ValueError("Unknown parent for " + item["slug"])
    return catalog


def export_catalog(
    root: Path, locations: list[dict[str, Any]], redirects: list[dict[str, Any]]
) -> dict[str, Any]:
    """Round-trip geography to Git's format; no content, reviews or runtime state."""
    fields = load_catalog(root)["locations"][0].keys()
    by_id = {row["id"]: row for row in locations}
    return {
        "locations": [
            {
                field: (by_id[row["parent_id"]]["slug"] if row["parent_id"] else None)
                if field == "parent_slug"
                else row[field]
                for field in fields
            }
            for row in sorted(locations, key=lambda row: row["id"])
        ],
        "redirects": [
            {"slug": row["slug"], "location_slug": by_id[row["location_id"]]["slug"]}
            for row in sorted(redirects, key=lambda row: row["slug"])
        ],
    }
