import json
from pathlib import Path

import pytest

from involta_guide.domain.location_slug import location_slug


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Переславль-Залесский", "pereslavl-zalessky"),
        ("Нижний Новгород", "nizhny-novgorod"),
        ("Великий Новгород", "veliky-novgorod"),
        ("Новый Уренгой", "novy-urengoy"),
        ("Старый Оскол", "stary-oskol"),
        ("Грозный", "grozny"),
        ("Горячий Ключ", "goryachy-klyuch"),
        ("Орёл", "orel"),
        ("Плёс", "ples"),
        ("Озёры", "ozery"),
        ("Химки", "himki"),
        ("Балашиха", "balashiha"),
        ("Благовещенск", "blagoveschensk"),
        ("Мытищи", "mytischi"),
        ("Щёлково", "schelkovo"),
        ("Йошкар-Ола", "yoshkar-ola"),
        ("Бийск", "biysk"),
        ("Санкт-Петербург", "spb"),
        (" Санкт — Петербург ", "spb"),
        ("МОСКВА", "moskva"),
        ("Орёл  /  центр", "orel-tsentr"),
        ("Плёс", "ples"),
    ],
)
def test_readable_location_slug(name, expected):
    assert location_slug(name) == expected


def test_catalogue_has_unique_urls_and_all_references_survive_rename():
    root = Path(__file__).resolve().parents[2]
    catalog = json.loads((root / "data/location-catalog.json").read_text())
    plan = json.loads((root / "migrations/data/readable_slugs.json").read_text())
    slugs = {row["slug"] for row in catalog["locations"]}
    assert len(slugs) == len(catalog["locations"]) == 1448
    for row in catalog["locations"]:
        assert row["parent_slug"] is None or row["parent_slug"] in slugs
    for row in catalog["related"]:
        assert row["location_slug"] in slugs and row["related_slug"] in slugs
    for row in catalog["redirects"]:
        assert row["location_slug"] in slugs and row["slug"] not in slugs
    for row in plan["locations"]:
        assert row["new"] in slugs and row["old"] not in slugs
