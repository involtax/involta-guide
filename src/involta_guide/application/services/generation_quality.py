"""Versioned generation contract, citation normalization and coverage accounting."""

import copy
import re
from typing import Any

from involta_guide.domain.entities import RuleViolation
from involta_guide.domain.plain_text import clean_editorial_text
from involta_guide.domain.rules import FACTOR_LABELS, FACTORS, score

CANDIDATE_LIMIT = 10
PUBLISHED_LIMIT = 3


def generation_content(raw: dict[str, Any], *, recorded: bool = False) -> dict[str, Any]:
    """Build the existing server views from one validated research record per candidate."""
    from involta_guide.application.commands.schemas import (
        GenerationResult,
        RecordedGenerationResult,
    )

    schema = RecordedGenerationResult if recorded else GenerationResult
    research = schema.model_validate(raw)
    cards, ranking, manifest = [], [], []
    for candidate in research.candidates:
        item = candidate.model_dump(mode="json")
        manifest.append(
            {
                key: item[key]
                for key in (
                    "name",
                    "mapsUrl",
                    "status",
                    "reason",
                    "sources",
                    "exclusionCode",
                    "verificationIssues",
                )
            }
        )
        if candidate.status == "verified":
            cards.append(
                {
                    key: item[key]
                    for key in (
                        "name",
                        "cuisine",
                        "description",
                        "mapsUrl",
                        "sources",
                        "phone",
                        "openingHours",
                        "latitude",
                        "longitude",
                    )
                }
            )
        if candidate.scores:
            ranking.append(
                {
                    "name": candidate.name,
                    "mapsUrl": candidate.mapsUrl,
                    "scores": candidate.scores,
                    "evidence": [
                        {
                            "factor": e.factor,
                            "reason": e.reason,
                            "confidence": e.confidence,
                            "sources": [candidate.sources[n - 1] for n in e.sourceRefs],
                        }
                        for e in candidate.evidence
                    ],
                }
            )
    return {
        "intro": research.intro,
        "discoveredRestaurants": [
            r.model_dump() for r in getattr(research, "discoveredRestaurants", [])
        ],
        "restaurants": cards,
        "rankingReport": ranking,
        "candidateReview": manifest,
        "warnings": research.warnings,
    }


def normalize_generation(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    result = copy.deepcopy(raw)
    links: list[str] = []
    rejected: list[str] = []
    changed = 0
    intro = result.get("intro")
    if isinstance(intro, str):
        text, urls, invalid = clean_editorial_text(intro)
        result["intro"] = text
        changed += text != intro
        links.extend(urls)
        rejected.extend(invalid)
    for card in result.get("restaurants", []):
        if not isinstance(card, dict) or not isinstance(card.get("description"), str):
            continue
        text, urls, invalid = clean_editorial_text(card["description"])
        changed += text != card["description"]
        card["description"] = text
        card["sources"] = list(dict.fromkeys([*card.get("sources", []), *urls]))
        links.extend(urls)
        rejected.extend(invalid)
    for candidate in result.get("candidateReview") or []:
        if isinstance(candidate, dict) and isinstance(candidate.get("reason"), str):
            text, urls, invalid = clean_editorial_text(candidate["reason"])
            changed += text != candidate["reason"]
            candidate["reason"] = text
            candidate["sources"] = list(dict.fromkeys([*candidate.get("sources", []), *urls]))
            links.extend(urls)
            rejected.extend(invalid)
    return result, {
        "cleanedTexts": changed,
        "extractedSources": list(dict.fromkeys(links)),
        "rejectedSources": list(dict.fromkeys(rejected)),
    }


def _key(name: str) -> str:
    return " ".join(name.casefold().split())


def _legacy_leads(warnings: list[str]) -> dict[str, dict[str, Any]]:
    leads: dict[str, dict[str, Any]] = {}
    for warning in warnings:
        discovery = re.search(
            r"(?:Широкий поиск включал|Поиск охватил|Найдены кандидаты)\s*(.+?)(?:\.\s|$)", warning
        )
        names = re.findall(r"«([^»]+)»", discovery[1]) if discovery else []
        for name in names:
            leads.setdefault(
                _key(name),
                {
                    "name": name,
                    "mapsUrl": None,
                    "status": "needs_verification",
                    "reason": "Упомянут в поиске, но проверка или причина исключения не зафиксированы.",
                    "sources": clean_editorial_text(warning)[1],
                },
            )
        excluded = re.match(r"«([^»]+)»\s+не включ[её]н", warning)
        if excluded:
            # Only this explicit geographic statement is migrated as an exclusion.
            # Ambiguous legacy reasons remain unverified leads.
            outside = "вне заданного охвата" in warning
            leads[_key(excluded[1])] = {
                "name": excluded[1],
                "mapsUrl": None,
                "status": "excluded" if outside else "needs_verification",
                "exclusionCode": "outside_area" if outside else None,
                "reason": clean_editorial_text(warning)[0],
                "sources": clean_editorial_text(warning)[1],
            }
    return leads


def assess_coverage(content: dict[str, Any], normalization: dict[str, Any]) -> dict[str, Any]:
    supplied = content.get("candidateReview")
    structured = supplied is not None
    candidates = copy.deepcopy(supplied or [])
    keys = [_key(row["name"]) for row in candidates]
    if len(set(keys)) != len(keys):
        raise RuleViolation("В candidateReview повторяется кандидат")
    by_name = {_key(row["name"]): row for row in candidates}
    report = {_key(row["name"]): row for row in content["rankingReport"]}
    cards = {_key(row["name"]): row for row in content["restaurants"]}
    issues: list[str] = []
    if not structured:
        issues.append(
            "В сохранённом ответе нет структурированного отчёта обхода; полнота не подтверждена."
        )
    # A structured report owns its shortlist. A passing mention in a warning is not a task.
    legacy = {} if structured else _legacy_leads(content.get("warnings", []))
    for name, lead in legacy.items():
        if name not in by_name:
            by_name[name] = lead
            if structured:
                issues.append(
                    "В отчёте обхода пропущено упомянутое в предупреждениях место: " + lead["name"]
                )
        elif lead["status"] == "excluded" and by_name[name]["status"] == "verified":
            raise RuleViolation("Противоречивый статус кандидата: " + lead["name"])
    for name, row in report.items():
        complete = score(row["scores"]) is not None and bool(row.get("mapsUrl")) and name in cards
        if name not in by_name:
            by_name[name] = {
                "name": row["name"],
                "mapsUrl": row.get("mapsUrl"),
                "status": "verified" if complete else "needs_verification",
                "reason": "Подтверждены карточка и восемь оценок."
                if complete
                else "Недостаточно данных для полного рейтинга.",
                "sources": [],
            }
            if structured:
                issues.append("В отчёте обхода пропущен кандидат: " + row["name"])
        elif not structured and complete:
            by_name[name].update(
                status="verified",
                reason="Подтверждены карточка и восемь оценок.",
                mapsUrl=row.get("mapsUrl"),
            )
    if not structured:
        for name, row in report.items():
            candidate = by_name[name]
            candidate["mapsUrl"] = candidate.get("mapsUrl") or row.get("mapsUrl")
            if candidate["status"] == "needs_verification":
                missing = [
                    label
                    for factor, label in zip(FACTORS, FACTOR_LABELS)
                    if row["scores"].get(factor) is None
                ]
                candidate["reason"] = (
                    "Не хватает оценок: " + ", ".join(missing) + "."
                    if missing
                    else "Нет подтверждённой карточки для публикации."
                )
                specific = next(
                    (
                        warning
                        for warning in content.get("warnings", [])
                        if warning.startswith("«" + row["name"] + "»:")
                    ),
                    None,
                )
                if specific:
                    candidate["reason"], candidate["sources"], _ = clean_editorial_text(specific)
    for name, candidate in by_name.items():
        if candidate["status"] == "excluded" and name in cards:
            raise RuleViolation("Исключённый кандидат попал в карточки: " + candidate["name"])
    if structured:
        for name, candidate in by_name.items():
            row = report.get(name)
            complete = (
                row is not None and score(row["scores"]) is not None and bool(row.get("mapsUrl"))
            )
            if candidate["status"] == "verified" and (not complete or name not in cards):
                raise RuleViolation(
                    "Проверенному кандидату нужны карточка и восемь оценок: " + candidate["name"]
                )
            if name in cards and candidate["status"] != "verified":
                raise RuleViolation("Карточка включена без статуса verified: " + candidate["name"])
    pending = [row for row in by_name.values() if row["status"] == "needs_verification"]
    if pending:
        issues.append("Нуждаются в проверке: " + ", ".join(row["name"] for row in pending))
    if normalization["rejectedSources"]:
        issues.append(
            "Из текста удалены ссылки, не соответствующие правилам источников; исходный ответ сохранён."
        )
    candidates = list(by_name.values())
    quality = {
        "version": 2,
        "status": "partial" if issues else "complete",
        "structuredCoverage": structured,
        "discovered": len(candidates),
        "verified": sum(c["status"] == "verified" for c in candidates),
        "excluded": sum(c["status"] == "excluded" for c in candidates),
        "needsVerification": len(pending),
        "publishedCards": len(content["restaurants"]),
        "issues": issues,
        **normalization,
    }
    return {**content, "candidateReview": candidates, "qualityReport": quality}
