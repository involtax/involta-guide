import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from involta_guide.application.commands.schemas import CandidateReview, ContentInput, Generation
from involta_guide.application.services.generation_quality import (
    assess_coverage,
    normalize_generation,
    prepare_generation_prompt,
)
from involta_guide.domain.entities import RuleViolation
from involta_guide.domain.plain_text import clean_editorial_text
from involta_guide.domain.rules import FACTORS, rank

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/pereslavl-generation-response.json"


def process(raw):
    normalized, details = normalize_generation(raw)
    content = Generation.model_validate(normalized).model_dump(mode="json")
    result = assess_coverage(rank(content), details)
    ContentInput.model_validate(result)
    return result


def test_actual_pereslavl_response_retains_all_leads_and_cleans_descriptions():
    raw = json.loads(FIXTURE.read_text())
    original = copy.deepcopy(raw)
    result = process(raw)
    assert raw == original
    quality = result["qualityReport"]
    assert quality["status"] == "partial" and not quality["structuredCoverage"]
    assert (
        quality["discovered"],
        quality["verified"],
        quality["excluded"],
        quality["needsVerification"],
    ) == (10, 2, 1, 7)
    names = {row["name"]: row for row in result["candidateReview"]}
    for name in ["Бояре", "Высоко", "Альбицкий сад", "Дом", "Лобиани", "Монпансье"]:
        assert names[name]["status"] == "needs_verification"
    assert names["Попов луг"]["exclusionCode"] == "outside_area"
    assert names["Попов луг"]["sources"]
    assert names["Гастробар 1152"]["status"] == "needs_verification"
    assert names["Гастробар 1152"]["mapsUrl"]
    assert "команды не установлены" in names["Гастробар 1152"]["reason"]
    gastro = next(row for row in result["rankingReport"] if row["name"] == "Гастробар 1152")
    assert gastro["scores"]["consistency"] is None and gastro["totalScore"] is None
    assert quality["cleanedTexts"] == 2
    assert len(result["restaurants"]) == 2
    for old, new in zip(original["restaurants"], result["restaurants"]):
        assert "](" not in new["description"] and "https://" not in new["description"]
        assert set(old["sources"]) <= set(new["sources"])
    assert "возможна очередь" in result["restaurants"][0]["description"]
    assert "ошибки подачи" in result["restaurants"][1]["description"]


def test_display_limit_never_allows_a_missing_lower_ranked_card():
    report = [
        {
            "name": name,
            "mapsUrl": f"https://yandex.ru/maps/{name}",
            "scores": dict.fromkeys(FACTORS, value),
        }
        for name, value in [("A", 9), ("B", 8), ("C", 7)]
    ]
    with pytest.raises(RuleViolation, match="C"):
        rank({"rankingReport": report, "restaurants": [{"name": "A"}, {"name": "B"}]}, 2)


def test_structured_pending_candidates_cannot_be_hidden_by_model_quality_claims():
    raw = json.loads(FIXTURE.read_text())
    raw["candidateReview"] = [
        {
            "name": row["name"],
            "mapsUrl": row["mapsUrl"],
            "status": "verified",
            "reason": "Есть карта и оценки",
            "sources": row["sources"],
        }
        for row in raw["restaurants"]
    ] + [
        {
            "name": "Не проверен",
            "status": "needs_verification",
            "reason": "Не закончена проверка источников",
        }
    ]
    raw["qualityReport"] = {"status": "complete", "verified": 100}
    result = process(raw)
    assert result["qualityReport"]["status"] == "partial"
    assert result["qualityReport"]["verified"] == 2
    assert any(row["name"] == "Не проверен" for row in result["candidateReview"])


def test_verified_claim_requires_complete_scores_and_card():
    raw = json.loads(FIXTURE.read_text())
    raw["candidateReview"] = [
        {"name": "Гастробар 1152", "status": "verified", "reason": "Модель заявила, что проверено"}
    ]
    with pytest.raises(RuleViolation, match="Гастробар 1152"):
        process(raw)


def test_complete_manifest_can_be_complete_with_a_small_legitimate_selection():
    raw = json.loads(FIXTURE.read_text())
    raw["rankingReport"] = raw["rankingReport"][:2]
    raw["warnings"] = []
    raw["candidateReview"] = [
        {
            "name": row["name"],
            "mapsUrl": row["mapsUrl"],
            "status": "verified",
            "reason": "Подтверждено источниками",
            "sources": row["sources"],
        }
        for row in raw["restaurants"]
    ] + [
        {
            "name": "Закрытый ресторан",
            "status": "excluded",
            "exclusionCode": "closed",
            "reason": "Официальный сайт сообщает о закрытии",
        }
    ]
    result = process(raw)
    assert result["qualityReport"]["status"] == "complete"
    assert result["qualityReport"]["verified"] == 2
    assert result["qualityReport"]["excluded"] == 1


@pytest.mark.parametrize(
    "reason,code", [(" ", "closed"), ("Не успели проверить", None), ("Нехватка данных", "budget")]
)
def test_exclusions_need_a_real_reason_and_supported_code(reason, code):
    with pytest.raises(ValidationError):
        CandidateReview(name="X", status="excluded", reason=reason, exclusionCode=code)


@pytest.mark.parametrize(
    ("text", "expected", "sources"),
    [
        ("Описание. ([сайт](https://example.com/menu))", "Описание.", ["https://example.com/menu"]),
        (
            "Читайте [меню](https://example.com/menu_(2026)).",
            "Читайте меню.",
            ["https://example.com/menu_(2026)"],
        ),
        (
            "Группа [1] и [меню](https://example.com).",
            "Группа [1] и меню.",
            ["https://example.com"],
        ),
        ("**Еда** и *сервис*.", "Еда и сервис.", []),
    ],
)
def test_plain_descriptions_keep_prose_and_move_citations(text, expected, sources):
    result, urls, rejected = clean_editorial_text(text)
    assert result == expected and urls == sources and not rejected


def test_untrusted_link_is_never_promoted_to_sources():
    text, urls, rejected = clean_editorial_text("Смотрите [меню](javascript:alert(1)).")
    assert text == "Смотрите меню." and not urls and rejected == ["javascript:alert(1)"]
    text, urls, rejected = clean_editorial_text("[Карта](https://[invalid)")
    assert not urls and rejected


def test_new_prompt_and_runtime_context_are_independent_of_display_limit():
    legacy = "Мой стиль. Размер подборки определяется полем {лимит}, заданным редактором. Твой результат — черновик. Решение о публикации принимает человек."
    current = prepare_generation_prompt(legacy)
    assert current.startswith("Мой стиль.")
    assert "{лимит}" not in current and "Размер подборки определяется" not in current
    assert "candidateReview" in current and "exclusionCode" in current
    prompt = (FIXTURE.parents[2] / "data/prompts/restaurant-ranking-v1.txt").read_text()
    assert "{лимит}" not in prompt and "Размер подборки определяется" not in prompt


def test_manifest_cannot_hide_extra_discovered_names_in_warnings():
    raw = json.loads(FIXTURE.read_text())
    raw["candidateReview"] = [
        {"name": row["name"], "status": "verified", "reason": "Есть оценки"}
        for row in raw["restaurants"]
    ]
    result = process(raw)
    assert result["qualityReport"]["status"] == "partial"
    assert any(row["name"] == "Бояре" for row in result["candidateReview"])


def test_html_is_plain_text_and_embedded_code_is_not_published():
    text, urls, rejected = clean_editorial_text(
        'Еда <b>вкусная</b>. <script>alert(1)</script><a href="https://example.com/menu">Меню</a>'
    )
    assert text == "Еда вкусная. Меню"
    assert urls == ["https://example.com/menu"] and not rejected
