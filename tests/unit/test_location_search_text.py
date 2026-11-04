import pytest

from involta_guide.domain.location_search import russian_layout, search_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  ПЛЁС  ", "плес"),
        ("Санкт—Петербург", "санкт петербург"),
        ("нижний  новгород", "нижний новгород"),
        ("%_\\\x00", ""),
    ],
)
def test_search_normalizes_names_and_separators(raw, expected):
    assert search_text(raw) == expected


def test_keyboard_fallback_keeps_cyrillic_and_corrects_latin_keys():
    assert russian_layout("cjxb") == "сочи"
    assert russian_layout("сочи") == "сочи"
