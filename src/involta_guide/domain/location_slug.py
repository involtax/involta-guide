"""Suggest a readable URL once; stored slugs remain explicitly editable."""

import json
import re
import unicodedata
from pathlib import Path

RULES = json.loads(Path(__file__).with_name("transliteration.json").read_text())


def location_slug(name: str) -> str:
    words = re.findall(r"[а-яёa-z0-9]+", unicodedata.normalize("NFKC", name).lower())
    canonical = "-".join(words)
    if canonical in RULES["exceptions"]:
        return str(RULES["exceptions"][canonical])
    translated = []
    for word in words:
        suffix = ""
        for ending, replacement in RULES["word_endings"].items():
            if word.endswith(ending):
                word, suffix = word[: -len(ending)], replacement
                break
        value = "".join(RULES["letters"].get(letter, letter) for letter in word) + suffix
        if value:
            translated.append(value)
    return "-".join(translated)
