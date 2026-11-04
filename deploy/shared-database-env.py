"""Keep previous Voice images usable after the one-way database consolidation.

New images use DATABASE_URL and schema voice. Previous images read
VOICE_DATABASE_URL, so keep it pointing to the same database with search_path.
The old source URL is backed up privately, never printed or restored on rollback.
"""

import os
import shutil
import sys
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


def update(root: Path, release: Path) -> None:
    values = dict(
        line.split("=", 1)
        for line in (root / ".env.production").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    parsed = urlsplit(values["DATABASE_URL"])
    path = root / ".env.voice"
    voice_values = dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    query = dict(parse_qsl(parsed.query))
    query["options"] = (
        query.get("options", "")
        + " -c search_path="
        + voice_values.get("VOICE_DATABASE_SCHEMA", "voice")
    )
    url = urlunsplit(parsed._replace(query=urlencode(query, quote_via=quote)))
    backup = release / "voice-env-before-shared-db"
    if not backup.exists():
        shutil.copyfile(path, backup)
        backup.chmod(0o600)
    lines = [
        line for line in path.read_text().splitlines() if not line.startswith("VOICE_DATABASE_URL=")
    ]
    lines.append("VOICE_DATABASE_URL=" + url)
    temporary = path.with_suffix(".shared-tmp")
    temporary.write_text("\n".join(lines) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, path)


if __name__ == "__main__":
    update(Path(sys.argv[1]), Path(sys.argv[2]))
