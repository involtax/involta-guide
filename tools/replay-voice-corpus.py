#!/usr/bin/env python3
"""Offline Sherpa comparison on retained original recordings, without database writes."""

import argparse
import hashlib
import json
from pathlib import Path

from involta_voice.config import Settings
from involta_voice.sherpa import LocalSherpa, options
from involta_voice.text import catalogue_forms, restore_names
from involta_voice.variants import PRIMARY_VARIANT


def catalogue(log: dict) -> tuple[str, list[str]]:
    location = (log.get("voice") or {}).get("location") or {}
    city = location.get("name", "")
    names = location.get("recognition_dictionary") or [
        r["name"] for r in location.get("restaurants", [])
    ]
    return city, sorted({n for n in [city, *names] if n})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("var/voice-corpus"))
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument("--reviews", type=int, nargs="+")
    parser.add_argument("--scores", type=float, nargs="+", default=[5.5])
    parser.add_argument("--search", type=int, default=8)
    parser.add_argument("--catalogue", choices=["saved", "latest"], default="saved")
    parser.add_argument("--extra-words", nargs="*", default=[])
    parser.add_argument("--output", type=Path, default=Path("var/voice-corpus/replay.json"))
    args = parser.parse_args()
    index = json.loads((args.corpus / "index.json").read_text())
    if index["errors"]:
        raise SystemExit("Corpus has download errors; sync it successfully first")
    selected = [r for r in index["reviews"] if not args.reviews or r["number"] in args.reviews]
    if args.reviews and set(args.reviews) - {r["number"] for r in selected}:
        raise SystemExit("Requested review is not in the retained corpus")
    latest = {}
    for item in index["reviews"]:
        log = json.loads((args.corpus / item["log_file"]).read_text())
        latest[item["slug"]] = catalogue(log)
    settings = Settings(
        _env_file=None,
        database_url="postgresql://unused:unused@localhost/unused",
        service_secret="offline-research-only-no-network-secret",
        asr_models_dir=args.models_dir,
    )
    provider = LocalSherpa(settings)
    rows = []
    try:
        for item in selected:
            if not item.get("audio_file"):
                raise SystemExit(f"No original audio for review {item['number']}")
            path = (args.corpus / item["audio_file"]).resolve()
            if not path.is_relative_to(args.corpus.resolve()):
                raise SystemExit("Audio path escapes corpus")
            audio = path.read_bytes()
            if hashlib.sha256(audio).hexdigest() != item["sha256"]:
                raise SystemExit("Original audio hash mismatch")
            log = json.loads((args.corpus / item["log_file"]).read_text())
            city, names = latest[item["slug"]] if args.catalogue == "latest" else catalogue(log)
            forms = catalogue_forms(names, city=city)
            for score in args.scores:
                request = options(
                    settings,
                    "gigaam",
                    PRIMARY_VARIANT,
                    [*names, *args.extra_words],
                    location_name=city,
                )
                request.update(hotwords_score=score, max_active_paths=args.search)
                if score == 0:
                    request["dictionary"] = []
                body = json.loads(provider.call(request, audio)["body"])
                provider.close()
                row = {
                    "review": item["number"],
                    "slug": item["slug"],
                    "request": request,
                    "catalogue": args.catalogue,
                    "extra_words": args.extra_words,
                    "saved_text": item.get("transcript"),
                    "publication_status": item.get("publication_status"),
                    "mode": "offline-research",
                    "raw_text": body["text"],
                    "formatted_text": restore_names(body["text"], names, forms=forms),
                    "audio_sha256": body["audio_sha256"],
                    "pcm_sha256": body["pcm_sha256"],
                    "segments": body.get("segments"),
                    "elapsed_seconds": body["elapsed_seconds"],
                }
                rows.append(row)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
                args.output.chmod(0o600)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        provider.close()


if __name__ == "__main__":
    main()
