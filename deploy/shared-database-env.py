"""Keep previous Voice images usable after the one-way database consolidation.

New images use DATABASE_URL and schema voice. Previous images read
VOICE_DATABASE_URL, so keep it pointing to the same database with search_path.
The old source URL is backed up privately, never printed or restored on rollback.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


def read_values(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, raw = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            raise ValueError("Invalid environment assignment (value withheld)")
        try:
            parts = shlex.split(raw, comments=False, posix=True)
        except ValueError:
            raise ValueError("Invalid environment quoting (value withheld)") from None
        if len(parts) > 1:
            raise ValueError("Environment values containing spaces must be quoted")
        values[key.strip()] = parts[0] if parts else ""
    return values


def update(root: Path, release: Path, *, recover: bool = False, check: bool = False) -> None:
    values = read_values((root / ".env.production").read_text())
    parsed = urlsplit(values["DATABASE_URL"])
    if parsed.scheme not in {"postgresql", "postgresql+psycopg", "postgres"} or not parsed.hostname:
        raise ValueError("Invalid PostgreSQL URL (value withheld)")
    path = root / ".env.voice"
    backup = release / "voice-env-before-shared-db"
    source = backup if recover else path
    original = source.read_text()
    voice_values = read_values(original)
    schema = voice_values.get("VOICE_DATABASE_SCHEMA", "voice")
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema):
        raise ValueError("Invalid Voice schema")
    query = dict(parse_qsl(parsed.query))
    query["options"] = query.get("options", "") + " -c search_path=" + schema
    url = urlunsplit(parsed._replace(query=urlencode(query, quote_via=quote))).replace("'", "%27")
    lines = [
        line
        for line in original.splitlines()
        if line.partition("=")[0].strip() != "VOICE_DATABASE_URL"
    ]
    lines.append("VOICE_DATABASE_URL='" + url + "'")
    # Parse the complete result before replacing the file needed for rollback.
    result = "\n".join(lines) + "\n"
    assert read_values(result)["VOICE_DATABASE_URL"] == url
    # Use the actual Compose parser before changing the env needed by rollback.
    # Output may contain secrets: capture it and report only the validation stage.
    with tempfile.NamedTemporaryFile(
        mode="w", dir=root, prefix=".voice-env-", delete=False
    ) as file:
        temporary = Path(file.name)
        file.write(result)
    try:
        validation = subprocess.run(
            [
                "docker",
                "compose",
                "--project-name",
                "involta-guide-env-validation",
                "--env-file",
                str(temporary),
                "--file",
                "-",
                "config",
                "--format",
                "json",
            ],
            input=json.dumps(
                {"services": {"validate": {"image": "busybox", "env_file": [str(temporary)]}}}
            ),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if validation.returncode:
            raise ValueError("Compose rejected candidate Voice environment (details withheld)")
        resolved = json.loads(validation.stdout)["services"]["validate"]["environment"]
        # Compose escapes literal dollars when serializing a reusable config.
        if resolved["VOICE_DATABASE_URL"].replace("$$", "$") != url:
            raise ValueError("Compose changed the candidate database URL")
        if check:
            return
        if not backup.exists():
            shutil.copyfile(path, backup)
            backup.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    update(
        Path(sys.argv[1]),
        Path(sys.argv[2]),
        recover="--recover" in sys.argv[3:],
        check="--check" in sys.argv[3:],
    )
