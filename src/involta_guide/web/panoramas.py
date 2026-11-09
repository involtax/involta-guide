"""Versioned artwork: a location's canonical slug is its image filename."""

import re
from pathlib import Path

PANORAMA_DIRECTORY = Path(__file__).parent / "static" / "panoramas"


def panorama_for(slug: str) -> str | None:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        return None
    filename = f"{slug}.avif"
    return f"/assets/panoramas/{filename}" if (PANORAMA_DIRECTORY / filename).is_file() else None
