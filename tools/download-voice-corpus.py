#!/usr/bin/env python3
"""Mirror retained beta reviews and their original audio, with verified hashes.

python tools/download-voice-corpus.py --base-url https://involta.ru/guide/ --output var/voice-corpus
Only GET requests. Reading never starts ASR. No credentials are needed during beta.
"""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

MARKER = "involta-voice-corpus-v1"
EXTENSIONS = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4", "audio/wav": "wav"}


def download(base: str, output: Path) -> dict:
    origin = urlsplit(base)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    previous = (
        json.loads((output / "index.json").read_text()) if (output / "index.json").exists() else {}
    )
    if previous and previous.get("managed_by") != MARKER:
        raise ValueError("Output already has an index belonging to another tool")

    def get(path: str) -> bytes:
        url = urljoin(base.rstrip("/") + "/", path)
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            raise ValueError("Export URLs must use the same origin")
        with urlopen(Request(url, headers={"User-Agent": MARKER}), timeout=60) as response:
            if urlsplit(response.url).netloc != origin.netloc:
                raise ValueError("Cross-origin redirect refused")
            data = response.read(32 * 1024 * 1024 + 1)
        if len(data) > 32 * 1024 * 1024:
            raise ValueError("Export response exceeds 32 MiB")
        return data

    def save(name: str, data: bytes) -> None:
        target = output / name
        temporary = output / (name + ".part")
        temporary.write_bytes(data)
        temporary.chmod(0o600)
        temporary.replace(target)

    reviews = []
    after = 0
    while True:
        page = json.loads(get(f"logs/reviews.json?after={after}&limit=100"))
        reviews.extend(page["reviews"])
        following = page["next_after"]
        if following is None:
            break
        if following <= after:
            raise ValueError("Export cursor did not advance")
        after = following
    retained_ids = {str(UUID(item["id"])) for item in reviews}
    records, errors, files = [], [], []
    for item in reviews:
        identity = str(UUID(item["id"]))
        label = f"{item['slug']}/{item['number']}"
        try:
            detail = json.loads(get(item["json_url"]))
            log_file = identity + ".json"
            save(log_file, json.dumps(detail, ensure_ascii=False, indent=2).encode())
            files.append(log_file)
            record = {**item, "log_file": log_file}
            if item.get("audio_url"):
                voice = detail.get("voice") or {}
                mime = voice.get("mime")
                if mime not in EXTENSIONS or not voice.get("sha256"):
                    raise ValueError("Voice metadata/hash unavailable; log was retained")
                audio_file = identity + "." + EXTENSIONS[mime]
                audio_path = output / audio_file
                expected = voice["sha256"]
                if (
                    not audio_path.exists()
                    or hashlib.sha256(audio_path.read_bytes()).hexdigest() != expected
                ):
                    data = get(item["audio_url"])
                    if hashlib.sha256(data).hexdigest() != expected:
                        raise ValueError("Original audio SHA256 mismatch")
                    save(audio_file, data)
                record.update(audio_file=audio_file, sha256=expected, mime=mime)
                files.append(audio_file)
            records.append(record)
            print(label + ": downloaded", flush=True)
        except HTTPError as exc:
            errors.append({"review": label, "error": f"HTTP {exc.code}"})
        except (OSError, ValueError, KeyError) as exc:
            errors.append({"review": label, "error": str(exc)})
    # Prune only files previously created by this tool, never arbitrary local data.
    for name in set(previous.get("files", [])) - set(files):
        if Path(name).name != name or Path(name).suffix not in {
            ".json",
            ".webm",
            ".wav",
            ".ogg",
            ".mp4",
        }:
            raise ValueError("Unsafe managed filename")
        UUID(Path(name).stem)
        if Path(name).stem not in retained_ids:
            (output / name).unlink(missing_ok=True)
        elif (output / name).exists():
            files.append(name)
    result = {
        "schema_version": 1,
        "managed_by": MARKER,
        "source": base,
        "downloaded_at": datetime.now(UTC).isoformat(),
        "reviews": records,
        "errors": errors,
        "files": files,
    }
    save("index.json", json.dumps(result, ensure_ascii=False, indent=2).encode())
    print(f"Saved {len(records)} retained reviews; {len(errors)} errors")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://involta.ru/guide/")
    parser.add_argument("--output", type=Path, default=Path("var/voice-corpus"))
    args = parser.parse_args()
    raise SystemExit(1 if download(args.base_url, args.output)["errors"] else 0)
