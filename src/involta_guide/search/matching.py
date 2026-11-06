"""Name-only matching. Regions, descriptions and private records never enter the index."""

import re
import unicodedata
from functools import lru_cache
from itertools import groupby
from typing import Any

import pymorphy3
from rapidfuzz.distance import DamerauLevenshtein

from involta_guide.domain.location_search import russian_layout
from involta_guide.domain.location_slug import RULES, location_slug

VERSION = "hybrid-names-v1"
LIMIT = 8
STOP_WORDS = frozenset(
    "где поесть покушать найти ресторан рестораны ресторане кафе в во на у около город городе города пожалуйста".split()
)
COMMON_ALIASES = {
    "санкт петербург": ("питер", "спб", "петербург", "saint petersburg", "st petersburg"),
    "москва": ("мск",),
}


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(re.findall(r"[^\W_]+", value))


@lru_cache(maxsize=1)
def morphology() -> pymorphy3.MorphAnalyzer:
    return pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=50000)
def forms(word: str) -> frozenset[str]:
    if not re.fullmatch("[а-я]+", word):
        return frozenset((word,))
    return frozenset(
        [word]
        + [
            normalize(p.normal_form)
            for p in morphology().parse(word)[:6]
            if p.is_known and p.score >= 0.1
        ]
    )


@lru_cache(maxsize=20000)
def names(name: str, aliases: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    canonical = normalize(name)
    variants = [(canonical, "name")]
    for alias in (*aliases, *COMMON_ALIASES.get(canonical, ())):
        if normalize(alias):
            variants.append((normalize(alias), "alias"))
    # The general spelling complements URL exceptions such as Санкт-Петербург → spb.
    latin = "".join(RULES["letters"].get(c, c) for c in canonical)
    variants.extend(
        ((normalize(latin), "transliteration"), (normalize(location_slug(name)), "transliteration"))
    )
    return tuple(dict.fromkeys(variants))


@lru_cache(maxsize=50000)
def word_match(query: str, candidate: str) -> tuple[int, int] | None:
    if query == candidate:
        return 0, 0
    if len(query) >= 2 and candidate.startswith(query):
        return 1, 0
    if len(query) >= 3 and forms(query) & forms(candidate):
        return 2, 0
    if len(query) >= 3 and query in candidate:
        return 3, 0
    if len(query) < 4 or len(candidate) < 4:
        return None
    allowed = 1 if len(query) < 7 else 2
    distance = DamerauLevenshtein.distance(query, candidate, score_cutoff=allowed)
    if distance <= allowed:
        return 4, distance
    return None


def variant_rank(query: str, name: str, qualifier: str) -> tuple[int, int] | None:
    if query == name:
        return 0, 0
    words = query.split()
    primary, context = name.split(), qualifier.split()
    costs = []
    touches_name = False
    for word in words:
        name_costs = [cost for part in primary if (cost := word_match(word, part)) is not None]
        context_costs = [cost for part in context if (cost := word_match(word, part)) is not None]
        if not name_costs and not context_costs:
            return None
        if name_costs and (not context_costs or min(name_costs) <= min(context_costs)):
            touches_name = True
            costs.append(min(name_costs))
        else:
            costs.append(min(context_costs))
    if not touches_name:
        return None
    return max(1, max(cost[0] for cost in costs)), sum(cost[1] for cost in costs)


def search(catalogue: list[dict[str, Any]], value: str, limit: int = LIMIT) -> list[dict[str, Any]]:
    original = normalize(value[:100])
    meaningful = " ".join(word for word in original.split() if word not in STOP_WORDS)
    if not meaningful:
        return []
    queries: list[tuple[str, str | None]] = [(meaningful, None)]
    alternate = russian_layout(meaningful)
    if alternate != meaningful:
        queries.append((alternate, "layout"))
    latin = normalize("".join(RULES["letters"].get(c, c) for c in meaningful))
    if latin != meaningful:
        queries.append((latin, "transliteration"))
    matches = []
    for order, row in enumerate(catalogue):
        best = None
        qualifiers = [""]
        if row["kind"] == "restaurant":
            qualifiers += [
                item[0]
                for item in names(row["location_name"], tuple(row.get("location_aliases") or []))
            ]
        for query, query_source in queries:
            for name, source in names(row["name"], tuple(row.get("aliases") or [])):
                # Preserve full brand names even when they include a generic word, e.g. «Кафе Пушкинъ».
                rank = (
                    (0, 0)
                    if original == name
                    else min(
                        (
                            candidate
                            for qualifier in qualifiers
                            if (candidate := variant_rank(query, name, qualifier)) is not None
                        ),
                        default=None,
                    )
                )
                if rank is None:
                    continue
                tier, distance = rank
                source_penalty = int(source != "name")
                key = (tier, distance, source_penalty, int(query_source is not None))
                if best is None or key < best[0]:
                    reason = (
                        query_source
                        if query_source
                        else source
                        if source != "name"
                        else ("exact", "prefix", "morphology", "contains", "typo")[tier]
                    )
                    best = (key, reason)
        if best is not None:
            public = {
                key: row[key] for key in ("id", "kind", "name", "location", "context", "anchor")
            }
            public["match"] = best[1]
            public["rank"] = list(best[0])
            matches.append((best[0], order, public))
    matches.sort(key=lambda item: (item[0], item[1]))
    ordered = []
    for _, group in groupby(matches, key=lambda item: item[0]):
        peers = [item[2] for item in group]
        locations = [item for item in peers if item["kind"] == "location"]
        restaurants = [item for item in peers if item["kind"] == "restaurant"]
        # Equal relevance: two locations, then one restaurant. Weak matches never jump ahead.
        for i in range(0, max(len(locations), 2 * len(restaurants)), 2):
            ordered.extend(locations[i : i + 2])
            ordered.extend(restaurants[i // 2 : i // 2 + 1])
    return ordered[:limit]
