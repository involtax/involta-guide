from uuid import UUID

import pytest

from involta_guide.domain.entities import RuleViolation
from involta_guide.review_analysis.names import restore_restaurant_names
from involta_guide.review_analysis.schema import Analysis

ID = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"


def result(source="Ресторан Папараццо в Выборге.", source_name="Папараццо", **mention):
    return Analysis.model_validate(
        {
            "category": "ok",
            "author_name": None,
            "edited_text": source,
            "mentions": [
                {
                    "name": "Paparazzi bistro",
                    "restaurant_id": ID,
                    "sentiment": 1,
                    "suggested": False,
                    "evidence": source,
                    "source_name": source_name,
                    **mention,
                }
            ],
            "reason": "Упоминание ресторана",
        }
    )


def test_resolved_name_is_replaced_without_changing_source_evidence_or_raw_analysis():
    raw = result()
    original = raw.model_dump()
    source = raw.edited_text
    after = restore_restaurant_names(raw, source, [{"id": ID, "name": "Paparazzi bistro"}])
    assert after.edited_text == "Ресторан Paparazzi bistro в Выборге."
    assert raw.model_dump() == original
    assert after.mentions[0].evidence == source
    assert after.mentions[0].source_name == "Папараццо"
    assert after.mentions[0].sentiment == 1
    assert after.category == raw.category


@pytest.mark.parametrize(
    "source",
    [
        "Папараццо, а потом опять Папараццо!",
        "«Папараццо» понравился.",
    ],
)
def test_repeated_mentions_and_punctuation(source):
    after = restore_restaurant_names(
        result(source), source, [{"id": ID, "name": "Paparazzi bistro"}]
    )
    assert after.edited_text == source.replace("Папараццо", "Paparazzi bistro")


def test_case_changed_by_editor_is_still_corrected_but_substrings_are_not():
    source = "Папараццо, Папараццовый и Папараццо."
    raw = result(source)
    raw.edited_text = source.lower()
    after = restore_restaurant_names(raw, source, [{"id": ID, "name": "Paparazzi bistro"}])
    assert after.edited_text == "Paparazzi bistro, папараццовый и Paparazzi bistro."


@pytest.mark.parametrize(
    "name,source_name,source,expected",
    [
        ("Алхимист", "алхимисте", "Был в алхимисте.", "Был в Алхимисте."),
        ("Горы", "Горах", "Ужинали в Горах.", "Ужинали в Горах."),
        (
            "Paparazzi bistro",
            "Paparazzi bistro",
            "Был в Paparazzi bistro.",
            "Был в Paparazzi bistro.",
        ),
    ],
)
def test_correct_catalogue_spelling_and_russian_cases(name, source_name, source, expected):
    raw = result(source, source_name)
    after = restore_restaurant_names(raw, source, [{"id": ID, "name": name}])
    assert after.edited_text == expected
    assert after.mentions[0].name == name


def test_unknown_restaurant_is_not_changed_or_assigned_an_id():
    raw = result(restaurant_id=None, name="Папараццо")
    after = restore_restaurant_names(raw, raw.edited_text, [{"id": ID, "name": "Paparazzi bistro"}])
    assert after.model_dump() == raw.model_dump()


def test_model_already_corrected_name_keeps_that_text():
    raw = result()
    raw.edited_text = "Ресторан Paparazzi bistro в Выборге."
    after = restore_restaurant_names(
        raw, raw.mentions[0].evidence, [{"id": ID, "name": "Paparazzi bistro"}]
    )
    assert after.edited_text == raw.edited_text


def test_legacy_response_without_source_name_remains_readable_without_guessed_edits():
    saved = result().model_dump()
    saved["mentions"][0].pop("source_name")
    raw = Analysis.model_validate(saved)
    after = restore_restaurant_names(raw, raw.edited_text, [{"id": ID, "name": "Paparazzi bistro"}])
    assert after.edited_text == raw.edited_text
    assert after.mentions[0].source_name is None


@pytest.mark.parametrize("category", ["bugs", "other"])
def test_site_error_quotes_are_not_rewritten_as_restaurant_reviews(category):
    source = "На сайте написано Папараццо, это ошибка."
    raw = result(source)
    raw.category = category
    after = restore_restaurant_names(raw, source, [{"id": ID, "name": "Paparazzi bistro"}])
    assert after.edited_text == source


@pytest.mark.parametrize("source_name", ["Папарацци", "Вообще другой ресторан"])
def test_source_name_must_be_in_original_evidence(source_name):
    raw = result(source_name=source_name)
    with pytest.raises(RuleViolation, match="Исходное название"):
        restore_restaurant_names(raw, raw.edited_text, [{"id": ID, "name": "Paparazzi bistro"}])


def test_same_spelling_cannot_resolve_to_different_restaurants():
    raw = result()
    second = raw.mentions[0].model_copy(update={"restaurant_id": UUID(OTHER)})
    raw.mentions.append(second)
    with pytest.raises(RuleViolation, match="разным названиям"):
        restore_restaurant_names(
            raw,
            raw.edited_text,
            [{"id": ID, "name": "Paparazzi bistro"}, {"id": OTHER, "name": "Paparazzi pizza"}],
        )


def test_replacements_do_not_cascade_between_restaurants():
    source = "Первый и Второй."
    raw = result(source, "Первый")
    raw.mentions.append(
        raw.mentions[0].model_copy(update={"restaurant_id": UUID(OTHER), "source_name": "Второй"})
    )
    after = restore_restaurant_names(
        raw, source, [{"id": ID, "name": "Второй"}, {"id": OTHER, "name": "Третий"}]
    )
    assert after.edited_text == "Второй и Третий."


def test_response_schema_is_strict_while_old_results_still_parse():
    schema = Analysis.response_schema()
    mention = schema["$defs"]["Mention"]
    assert set(mention["required"]) == set(mention["properties"])
    assert "default" not in mention["properties"]["source_name"]
    assert {"type": "null"} in mention["properties"]["source_name"]["anyOf"]
    assert mention["additionalProperties"] is False
