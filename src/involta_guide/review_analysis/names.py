"""Restore catalogue spelling only for restaurant identities resolved by analysis."""

import re

from involta_guide.domain.entities import RuleViolation
from involta_voice.text import catalogue_forms, morphology

from .schema import Analysis


def restore_restaurant_names(
    result: Analysis, source: str, restaurants: list[dict[str, str]]
) -> Analysis:
    result = result.model_copy(deep=True)
    names = {str(item["id"]): item["name"] for item in restaurants}
    replacements: dict[str, str] = {}
    for mention in result.mentions:
        if mention.source_name is not None and (
            mention.source_name not in mention.evidence or mention.source_name not in source
        ):
            raise RuleViolation("Исходное название ресторана отсутствует в цитате отзыва")
        if not mention.restaurant_id:
            continue
        canonical = names.get(str(mention.restaurant_id))
        if canonical is None:
            raise RuleViolation("ИИ вернул ресторан вне переданного каталога")
        if mention.source_name and result.category == "ok":
            # Preserve a valid Russian inflection instead of forcing nominative.
            spelling = catalogue_forms([canonical]).get(
                mention.source_name.lower().replace("ё", "е")
            )
            if (
                spelling is None
                and canonical.istitle()
                and re.fullmatch("[А-Яа-яЁё]+", mention.source_name)
            ):
                # Identity is already resolved, so a predicted noun inflection
                # of that exact lemma is safe even for an unknown brand.
                if any(
                    p.tag.POS == "NOUN" and p.normal_form == canonical.lower()
                    for p in morphology().parse(mention.source_name)
                ):
                    spelling = mention.source_name.capitalize()
            spelling = spelling or canonical
            key = mention.source_name.lower()
            previous = replacements.get(key)
            if previous is not None and previous != spelling:
                raise RuleViolation("Одно упоминание сопоставлено разным названиям ресторанов")
            replacements[key] = spelling
        mention.name = canonical
    if replacements:
        # A single substitution avoids cascading A→B→C and partial word matches.
        pattern = (
            r"(?<!\w)(?:"
            + "|".join(re.escape(name) for name in sorted(replacements, key=len, reverse=True))
            + r")(?!\w)"
        )
        result.edited_text = re.sub(
            pattern,
            lambda match: replacements[match[0].lower()],
            result.edited_text,
            flags=re.IGNORECASE,
        )
        if len(result.edited_text) > 20000:
            raise RuleViolation("Текст после восстановления названий превышает лимит")
    return result
