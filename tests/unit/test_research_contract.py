import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from involta_guide.application.commands.schemas import GenerationResult
from involta_guide.application.services.generation_quality import (
    assess_coverage,
    generation_content,
    normalize_generation,
)
from involta_guide.domain.rules import FACTORS, rank


def research():
    return {
        "intro": "В Иванове хочется задержаться за ужином.",
        "candidates": [
            {
                "name": "Место",
                "status": "verified",
                "reason": "Подтверждены кухня и контакты",
                "cuisine": "Русская",
                "description": "Тихий ужин после прогулки.",
                "mapsUrl": "https://yandex.ru/maps/org/place/123/",
                "phone": "+74951234567",
                "sources": ["https://example.com/menu"],
                "scores": dict.fromkeys(FACTORS, 8),
                "evidence": [
                    {
                        "factor": f,
                        "reason": "Есть конкретные подтверждения",
                        "confidence": "medium",
                        "sourceRefs": [1],
                    }
                    for f in FACTORS
                ],
            }
        ],
        "warnings": [],
    }


def test_one_research_record_builds_consistent_server_views_without_admin_fields():
    raw = research()
    normalized, info = normalize_generation(generation_content(raw))
    content = assess_coverage(rank(normalized), info)
    assert content["restaurants"][0]["name"] == content["rankingReport"][0]["name"]
    assert content["candidateReview"][0]["status"] == "verified"
    assert content["rankingReport"][0]["totalScore"] == 8
    assert content["rankingReport"][0]["evidence"][0]["sources"] == raw["candidates"][0]["sources"]
    schema = json.dumps(GenerationResult.model_json_schema())
    for forbidden in ("alexStar", "locationId", "locationName", "qualityReport", "totalScore"):
        assert forbidden not in schema
    assert (
        "id"
        not in GenerationResult.model_json_schema()["$defs"]["GenerationCandidate"]["properties"]
    )


@pytest.mark.parametrize("field,value", [("alexStar", True), ("id", "a1"), ("locationId", "a2")])
def test_provider_cannot_supply_administrative_fields(field, value):
    raw = research()
    raw["candidates"][0][field] = value
    with pytest.raises(ValidationError, match=field):
        generation_content(raw)


def test_source_references_and_verified_claims_are_checked():
    raw = research()
    raw["candidates"][0]["evidence"][0]["sourceRefs"] = [2]
    with pytest.raises(ValidationError, match="sourceRefs"):
        generation_content(raw)
    raw = research()
    raw["candidates"][0]["phone"] = ""
    with pytest.raises(ValidationError, match="verified"):
        generation_content(raw)


def test_pending_candidate_with_full_scores_and_missing_phone_is_retained_not_published():
    raw = research()
    pending = deepcopy(raw["candidates"][0])
    pending.update(
        name="Пока без телефона",
        status="needs_verification",
        phone="",
        reason="Номер не подтверждён",
        verificationIssues=["phone"],
    )
    raw["candidates"].append(pending)
    content, info = normalize_generation(generation_content(raw))
    result = assess_coverage(rank(content), info)
    assert len(result["restaurants"]) == 1
    assert len(result["rankingReport"]) == 2
    assert result["qualityReport"]["needsVerification"] == 1
    assert result["qualityReport"]["status"] == "partial"


def test_new_request_cannot_turn_the_shortlist_into_a_city_directory():
    raw = research()
    raw["candidates"] = [dict(deepcopy(raw["candidates"][0]), name=f"Место {i}") for i in range(11)]
    with pytest.raises(ValidationError, match="at most 10"):
        generation_content(raw)
    assert GenerationResult.model_json_schema()["properties"]["candidates"]["maxItems"] == 10


def test_unknown_optional_facts_do_not_block_an_evidenced_restaurant():
    raw = research()
    raw["candidates"][0].update(openingHours=None, latitude=None, longitude=None)
    content = generation_content(raw)
    assert content["restaurants"][0]["openingHours"] is None
    assert content["candidateReview"][0]["verificationIssues"] == []


def test_pending_status_requires_a_real_publication_blocker_and_preserves_address_doubt():
    raw = research()
    candidate = raw["candidates"][0]
    candidate.update(status="needs_verification", reason="Часы неизвестны")
    with pytest.raises(ValidationError, match="verificationIssues"):
        generation_content(raw)
    candidate["verificationIssues"] = ["hours"]
    with pytest.raises(ValidationError):
        generation_content(raw)
    candidate.update(verificationIssues=["location"], reason="Источники указывают разные филиалы")
    content = generation_content(raw)
    assert content["restaurants"] == []
    assert len(content["rankingReport"]) == 1
    assert content["candidateReview"][0]["verificationIssues"] == ["location"]


def test_a_numeric_rating_without_evidence_cannot_be_published():
    raw = research()
    raw["candidates"][0]["evidence"][0]["sourceRefs"] = []
    with pytest.raises(ValidationError, match="источника"):
        generation_content(raw)
    raw = research()
    raw["candidates"][0]["verificationIssues"] = ["phone"]
    with pytest.raises(ValidationError, match="несовместим"):
        generation_content(raw)


def test_paid_v3_response_retains_its_recorded_contract_without_starting_a_new_request():
    raw = research()
    raw["candidates"] += [
        {"name": f"Упоминание {i}", "status": "needs_verification", "reason": "Не проверено"}
        for i in range(10)
    ]
    with pytest.raises(ValidationError):
        generation_content(raw)
    assert len(generation_content(raw, recorded=True)["candidateReview"]) == 11
