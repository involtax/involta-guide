import pytest

from involta_guide.search.matching import search


def record(name, *, kind="location", city="", aliases=()):
    return dict(
        id=kind + ":" + name,
        kind=kind,
        name=name,
        location="test",
        context=city,
        anchor="",
        aliases=list(aliases),
        location_name=city,
        location_aliases=[],
    )


@pytest.mark.parametrize(
    "query,name,kwargs",
    [
        ("в Иванове", "Иваново", {}),
        ("где поесть в Плёсе", "Плёс", {}),
        ("ИВАНОВО", "Иваново", {}),
        ("ples", "Плёс", {}),
        ("санкт петербург", "Санкт-Петербург", {}),
        ("питер", "Санкт-Петербург", {}),
        ("sankt peterburg", "Санкт-Петербург", {}),
        ("спб", "Санкт-Петербург", {}),
        ("cjxb", "Сочи", {}),
        ("Старое имя", "Новое имя", {"aliases": ["Старое имя"]}),
        ("Пушкни", "Пушкин", {"kind": "restaurant", "city": "Тула"}),
        ("Пушкин в Туле", "Пушкин", {"kind": "restaurant", "city": "Тула"}),
        ("Туле Пушкин", "Пушкин", {"kind": "restaurant", "city": "Тула"}),
        ("pushkin", "Пушкин", {"kind": "restaurant", "city": "Тула"}),
        ("Пушкин", "Pushkin", {"kind": "restaurant", "city": "Тула"}),
        ("Кафе Пушкинъ", "Кафе Пушкинъ", {"kind": "restaurant", "city": "Москва"}),
    ],
)
def test_human_name_queries(query, name, kwargs):
    result = search([record(name, **kwargs)], query)
    assert result and result[0]["name"] == name


def test_qualifier_cannot_replace_name_and_short_queries_do_not_fuzzy_match():
    restaurant = record("Пушкин", kind="restaurant", city="Тула")
    assert search([restaurant], "Тула") == []
    assert search([restaurant], "Пушкин Москва") == []
    assert search([record("Сочи")], "сч") == []
    assert search([record("Тест")], "где поесть") == []
    assert search([record("Тест")], "%%%") == []


def test_exact_restaurant_ranks_above_a_city_prefix_and_rank_explains_typos():
    rows = [record("Пушкинские Горы"), record("Пушкин", kind="restaurant", city="Тула")]
    result = search(rows, "Пушкин")
    assert result[0]["kind"] == "restaurant"
    assert result[0]["match"] == "exact"
    assert search(rows, "Пушкни")[0]["match"] == "typo"
    assert all("aliases" not in row and "location_name" not in row for row in result)


def test_equal_quality_does_not_hide_restaurants_behind_many_city_prefixes():
    rows = [record("Пушкин " + str(i)) for i in range(20)]
    rows += [record("Пушкин ресторан " + str(i), kind="restaurant", city="Тула") for i in range(4)]
    result = search(rows, "Пушк")
    assert len(result) == 8
    assert result[2]["kind"] == "restaurant" and result[5]["kind"] == "restaurant"


def test_full_name_wins_over_one_exact_word_inside_a_longer_name():
    rows = [record("Новые Горы"), record("Горы")]
    result = search(rows, "Горы")
    assert result[0]["name"] == "Горы" and result[0]["match"] == "exact"
    assert result[1]["match"] == "prefix"
