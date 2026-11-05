import pytest

from involta_voice.text import (
    catalogue_forms,
    clean_review_text,
    join_restaurant_fragments,
    remove_hesitations,
    restore_names,
)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("Никита. Э-э, хочу оставить отзыв.", "Никита. Хочу оставить отзыв."),
        ("Чем, а-а... По рейтингу выше.", "Чем... По рейтингу выше."),
        ("Хочу э-э, оставить отзыв.", "Хочу оставить отзыв."),
        ("А-а-а, хочу рамен.", "Хочу рамен."),
        ("эээ, вкусно", "Вкусно"),
        ("Э‑э, вкусно", "Вкусно"),
        ("Привет. Э-э. Хорошо.", "Привет. Хорошо."),
        ("Э-э. хороший ресторан.", "Хороший ресторан."),
        ("Э-э, а-а, хочу рамен.", "Хочу рамен."),
        ("А ресторан хороший.", "А ресторан хороший."),
        ("Вот это рамен, а не рыба.", "Вот это рамен, а не рыба."),
        ("Ну, эм, как бы неплохо.", "Ну, эм, как бы неплохо."),
        ("Аахен, А-Абакан, э-эклер", "Аахен, А-Абакан, э-эклер"),
        ("Текст без междометий...", "Текст без междометий..."),
        ("эээ", ""),
    ],
)
def test_hesitations_preserve_words_and_sentence_pauses(before, after):
    assert remove_hesitations(before) == after
    assert remove_hesitations(after) == after


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "Также ресторан. Горы, который недавно открылись.",
            "Также ресторан Горы, который недавно открылись.",
        ),
        (
            "Есть варианты. Ещё ресторан. Терраса хорош.",
            "Есть варианты. Ещё ресторан Терраса хорош.",
        ),
        ("Также ресторан. White Rabbit хорош.", "Также ресторан White Rabbit хорош."),
        (
            "Также ресторан. Горы. Ещё ресторан. Терраса.",
            "Также ресторан Горы. Ещё ресторан Терраса.",
        ),
        (
            "Мне понравился ресторан. Горы видны из окна.",
            "Мне понравился ресторан. Горы видны из окна.",
        ),
        (
            "Также ресторан хороший. Горы видны из окна.",
            "Также ресторан хороший. Горы видны из окна.",
        ),
        ("Также ресторан. Горыныч хорош.", "Также ресторан. Горыныч хорош."),
        ("Также ресторан. Горы-Горы хорош.", "Также ресторан. Горы-Горы хорош."),
        ("Также ресторан. Новый вариант.", "Также ресторан. Новый вариант."),
        ("Также ресторан? Горы видны?", "Также ресторан? Горы видны?"),
    ],
)
def test_restaurant_fragment_join_requires_known_name_and_unfinished_listing(before, after):
    names = ["Горы", "Терраса", "White Rabbit"]
    assert join_restaurant_fragments(before, names) == after
    assert join_restaurant_fragments(after, names) == after
    assert join_restaurant_fragments(before, []) == before


def test_cleanup_runs_after_catalogue_spelling_and_is_idempotent():
    names = ["Алхимист", "Горы"]
    restored = restore_names("Э-э, ресторан алхимист. Также ресторан. горы.", names)
    after = clean_review_text(restored, names)
    assert after == "Ресторан Алхимист. Также ресторан Горы."
    assert clean_review_text(after, names) == after


def test_dictionary_spelling_changes_only_complete_exact_names():
    assert (
        restore_names(
            "плёс, плюс, плёс равно плёс в квадрате. В усадьба плёс и ГРАНДЪ СОВА.",
            ["Плёс", "Грандъ Сова", "Усадьба Плёс"],
        )
        == "Плёс, плюс, Плёс равно Плёс в квадрате. В Усадьба Плёс и Грандъ Сова."
    )
    assert restore_names("в плёсе и плёсовый", ["Плёс"]) == "в плёсе и плёсовый"
    assert restore_names("плюс и плес", ["Плёс"]) == "плюс и плес"
    assert restore_names("до грандъ   сова!", ["Грандъ Сова"]) == "до Грандъ Сова!"
    assert restore_names("слова", []) == "слова"


@pytest.mark.parametrize(
    ("city", "before", "after"),
    [
        (
            "Иваново",
            "в иванове, из иванова, к иванову, под ивановом",
            "в Иванове, из Иванова, к Иванову, под Ивановом",
        ),
        ("Плёс", "В плёсе есть ещё и ресторан Софи.", "В Плёсе есть ещё и ресторан Софи."),
        (
            "Плёс",
            "плёс, плеса, плесу, плесом, плесе, плюс",
            "Плёс, Плёса, Плёсу, Плёсом, Плёсе, плюс",
        ),
        ("Ярославль", "из ярославля в ярославле", "из Ярославля в Ярославле"),
        ("Москва", "из москвы в москве", "из Москвы в Москве"),
        ("Орёл", "из орла в орле", "из Орла в Орле"),
        (
            "Нижний Новгород",
            "в нижнем новгороде, из нижнего новгорода, через нижний новгород",
            "в Нижнем Новгороде, из Нижнего Новгорода, через Нижний Новгород",
        ),
        ("Великие Луки", "в великих луках", "в Великих Луках"),
        ("Сочи", "в сочи", "в Сочи"),
        ("Крым", "в крыму", "в Крыму"),
    ],
)
def test_dictionary_inflections_preserve_case_not_nominative(city, before, after):
    forms = catalogue_forms([city], city=city)
    assert restore_names(before, [city], forms=forms) == after


def test_inflections_never_include_derived_adjectives_or_guessed_brands():
    names = [
        "Иваново",
        "Плёс",
        "Москва",
        "Нижний Новгород",
        "Алхимист",
        "Грандъ Сова",
        "Усадьба Плёс",
    ]
    forms = catalogue_forms(names, city="Иваново")
    text = "ивановский, плёсовый, москвич, плюс, всплеск, алхимисте"
    assert restore_names(text, names, forms=forms) == text
    assert "нижнего новгород" not in forms
    assert (
        restore_names("в грандъ сове, в усадьбе плёс, в алхимист", names, forms=forms)
        == "в Грандъ Сове, в Усадьбе Плёс, в Алхимист"
    )
    assert restore_names("иваново", names, forms=forms) == "Иваново"
    assert restore_names("в плесе", names, forms=forms) == "в Плёсе"


def test_old_exact_matching_keeps_first_catalogue_spelling():
    assert restore_names("плёс", ["Плёс", "плёс"]) == "Плёс"
