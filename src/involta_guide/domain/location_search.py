import re
import unicodedata
from typing import Any


def search_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value[:100]).lower().replace("ё", "е")
    return " ".join(re.findall(r"[^\W_]+", value))


def russian_layout(value: str) -> str:
    if not re.fullmatch(r"[a-z ]+", value):
        return value
    return search_text(
        value.translate(str.maketrans("qwertyuiopasdfghjklzxcvbnm", "йцукенгшщзфывапролдячсмить"))
    )


def trigrams(value: str) -> set[str]:
    result: set[str] = set()
    for word in value.split():
        padded = "  " + word + " "
        result.update(padded[i : i + 3] for i in range(len(padded) - 2))
    return result


def search_locations(catalogue: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Name-only search; catalogue order breaks ties. Mirrored in location-index.js."""
    query = search_text(query)
    if not query:
        return []
    names = [search_text(row["name"]) for row in catalogue]

    def direct(value: str) -> list[dict[str, Any]]:
        words = value.split()
        matches = []
        short = len(value.replace(" ", "")) < 3
        for index, name in enumerate(names):
            if not (name.startswith(value) if short else all(word in name for word in words)):
                continue
            rank = (
                0
                if name == value
                else 1
                if all(any(part.startswith(word) for part in name.split()) for word in words)
                else 2
            )
            matches.append((rank, index))
        return [catalogue[index] for _, index in sorted(matches)]

    matches = direct(query)
    if matches:
        return matches
    alternate = russian_layout(query)
    if alternate != query:
        matches = direct(alternate)
        if matches:
            return matches
    if len(alternate) < 4:
        return []
    wanted = trigrams(alternate)
    similar = []
    for index, name in enumerate(names):
        grams = trigrams(name)
        score = len(wanted & grams) / len(wanted | grams)
        if score >= 0.45:
            similar.append((-score, index))
    return [catalogue[index] for _, index in sorted(similar)]
