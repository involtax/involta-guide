import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from involta_guide.application.ports.repositories import Record, UowFactory
from involta_guide.domain.entities import Conflict, NotFound, RuleViolation

from .editorial import current_content, require


class LocationService:
    def __init__(self, uow: UowFactory) -> None:
        self.uow = uow

    def save(self, key: int | None, values: Record) -> int:
        with self.uow() as uow:
            repo = uow.repo
            target = key
            data = values.copy()
            if data["slug"] in {"new", "export"}:
                raise RuleViolation("Этот URL зарезервирован для служебного раздела")
            if any(row["id"] != target for row in repo.rows("locations", {"slug": data["slug"]})):
                raise RuleViolation("Этот URL уже занят другой локацией")
            if key:
                require(repo, "locations", key, lock=True)
            if target is not None and data["parent_id"] == target:
                raise RuleViolation("Локация не может ссылаться на себя")
            if data["parent_id"]:
                parent = require(repo, "locations", data["parent_id"])
                if data["active"] and not parent["active"]:
                    raise RuleViolation("Сначала включите родительскую локацию")
                if target in {row["id"] for row in repo.tree(data["parent_id"], ancestors=True)}:
                    raise RuleViolation("Нельзя создавать цикл")
            children = [r for r in repo.tree(key)[1:] if r["active"]] if key else []
            if not data["active"] and children:
                raise RuleViolation("Сначала отключите дочерние локации")
            # is_hub is explicit; the hierarchy remains navigation, never shared ownership.
            if key:
                current = require(repo, "locations", key, lock=True)
                if current["reset_pending"]:
                    raise Conflict("Сброс не завершён")
            data["is_hub"] = bool(data["is_hub"])
            if key:
                repo.change("locations", key, data)
            else:
                target = int(repo.add("locations", data)["id"])
            assert target is not None
            repo.audit("location", target, json.dumps(values, ensure_ascii=False))
            return int(target)

    def nearby(self, key: int, other: int, *, remove: bool = False) -> None:
        with self.uow() as uow:
            repo = uow.repo
            location = require(repo, "locations", key, lock=True)
            require(repo, "locations", other)
            if key == other:
                raise RuleViolation("Локация не может ссылаться на себя")
            before = location["nearby_location_ids"]
            after = (
                [value for value in before if value != other]
                if remove
                else list(dict.fromkeys([*before, other]))
            )
            if after == before:
                return
            repo.change("locations", key, {"nearby_location_ids": after})
            repo.audit("location_nearby", key, json.dumps({"before": before, "after": after}))

    def save_intro(self, key: int, text: str, expected: str | None, actor: str) -> bool:
        if len(text) > 30000 or "\x00" in text:
            raise RuleViolation("Вступление: до 30000 символов, без нулевого символа")
        with self.uow() as uow:
            repo = uow.repo
            location = require(repo, "locations", key, lock=True)
            if location["is_hub"] or location["reset_pending"]:
                raise Conflict("Локация не принимает изменение вступления")
            from involta_guide.catalogue.service import digest

            if expected not in (digest(location["intro"]), location["revision"]):
                raise Conflict(
                    "Вступление изменено другим действием. Сравните тексты и сохраните снова."
                )
            if location["intro"] == text:
                return False
            repo.change(
                "locations",
                key,
                {
                    "intro": text,
                    "revision": str(uuid4()),
                    "updated_at": datetime.now(UTC),
                },
            )
            repo.audit(
                "manual_intro",
                key,
                json.dumps(
                    {
                        "admin_session_id": actor,
                        "diff": [{"path": ["intro"], "before": location["intro"], "after": text}],
                    },
                    ensure_ascii=False,
                ),
            )
            return True


class Queries:
    def __init__(self, uow: UowFactory) -> None:
        self.uow = uow

    def health(self) -> None:
        with self.uow() as uow:
            uow.repo.health()

    def directory(self, **options: Any) -> Record:
        with self.uow() as uow:
            return uow.repo.search_locations(**options)

    def public_location_catalogue(self) -> list[Record]:
        with self.uow() as uow:
            return uow.repo.public_location_catalogue()

    def overview(self) -> Record:
        with self.uow() as uow:
            return uow.repo.summary()

    def get(self, table: str, key: str | int) -> Record:
        with self.uow() as uow:
            return require(uow.repo, table, key)

    def rows(self, table: str, filters: Record | None = None, *, limit: int = 200) -> list[Record]:
        with self.uow() as uow:
            return uow.repo.rows(table, filters, limit=limit)

    def location(self, key: int) -> Record:
        from .editorial import current_content

        with self.uow() as uow:
            repo = uow.repo
            location = require(repo, "locations", key)
            runs = repo.rows(
                "jobs", {"location_id": key, "kind": "generate", "applied": True}, limit=1
            )
            report = runs[0]["report"] if runs else {}
            return {
                "location": location,
                "content": {**report, **current_content(repo, location)},
                "available": repo.available(key),
                "nearby": repo.related(key),
                "parents": repo.tree(key, ancestors=True),
            }

    def admin_location(self, slug: str) -> Record:
        with self.uow() as uow:
            rows = uow.repo.rows("locations", {"slug": slug}, limit=1)
            if not rows:
                raise NotFound("Локация не найдена")
            key = rows[0]["id"]
            events = uow.repo.rows(
                "audit", {"action": "manual_intro", "target": str(key)}, limit=1000
            )
            for event in events:
                event["data"] = json.loads(event["detail"])
            runs = uow.repo.rows("jobs", {"location_id": key, "kind": "generate"}, limit=1)
            generation = runs[0] if runs else None
            if generation:
                logs = uow.repo.rows("request_logs", {"job_id": generation["id"]}, limit=1)
                generation["number"] = logs[0]["number"] if logs else None
        return {**self.location(key), "intro_events": events, "generation": generation}

    def restaurant(self, number: int) -> Record:
        with self.uow() as uow:
            rows = uow.repo.rows("restaurants", {"number": number}, limit=1)
            if not rows:
                raise NotFound("Ресторан не найден")
            card = rows[0]
        result = self.restaurant_browser(location_id=card["location_id"])
        result["restaurant_cards"] = [
            r for r in result["restaurant_cards"] if r["id"] == card["id"]
        ]
        return result

    def restaurant_browser(self, query: str = "", location_id: int | None = None) -> Record:
        from involta_guide.domain.rules import (
            FACTOR_LABELS,
            FACTORS,
            restaurant_rank_key,
            restaurant_score,
        )

        with self.uow() as uow:
            repo = uow.repo
            cities = repo.restaurant_cities(query)
            selected = next((c for c in cities if c["id"] == location_id), None)
            # City search filters the sidebar, never the explicitly selected location.
            if selected is None and location_id:
                selected = require(repo, "locations", location_id)
            if selected is None and not location_id and cities:
                selected = cities[0]
            result: Record = {
                "restaurant_cities": cities,
                "restaurant_city": selected,
                "restaurant_cards": [],
                "factors": list(zip(FACTORS, FACTOR_LABELS)),
            }
            if selected is None:
                return result
            cards = repo.city_restaurants(selected["id"])
            runs = repo.rows(
                "jobs",
                {"location_id": selected["id"], "kind": "generate", "applied": True},
                limit=1,
            )
            report = runs[0]["report"] if runs else {}
            result["generation_quality"] = report.get("qualityReport")
            result["candidate_review"] = report.get("candidateReview")

            for card in cards:
                approved = [
                    entry
                    for review in card["reviews"] or []
                    if review["moderation"] == "approved" and not review["fraud"]
                    for entry in review["entries"] or []
                ]
                card["review_scores"] = {
                    key: (sum(values) / len(values) if values else None)
                    for key in ("rating", "sentiment", "recommendation")
                    for values in [[e[key] for e in approved if e.get(key) is not None]]
                }
                card["review_count"] = len(approved)
                card["score"] = restaurant_score(card)
            cards.sort(key=restaurant_rank_key)
            for index, card in enumerate(cards, start=1):
                card["display_rank"] = index if card["active"] else None
            result["restaurant_cards"] = cards
            return result

    def public(self, slug: str) -> Record | None:
        with self.uow() as uow:
            repo = uow.repo
            found = repo.by_slug(slug)
            if not found:
                return None
            location, redirect = found
            tree = repo.tree(location["id"])
            useful = {r["id"] for r in tree if r["initialized_at"] is not None}
            # Hidden locations are visible only if their subtree has published content.
            for item in reversed(tree):
                if item["id"] in useful and item["parent_id"]:
                    useful.add(item["parent_id"])
            children = [
                r
                for r in tree
                if r["parent_id"] == location["id"]
                and r["active"]
                and (r["listed"] or r["id"] in useful)
            ]
            for child in children:
                child["child_count"] = sum(
                    r["parent_id"] == child["id"] and r["active"] for r in tree
                )
            related = repo.related(location["id"])
            return {
                "location": location,
                "redirect": redirect,
                "parents": repo.tree(location["id"], ancestors=True),
                "children": children,
                "related": related,
                "content": current_content(repo, location),
                "indexable": bool(useful),
            }

    def sitemap(self) -> list[Record]:
        with self.uow() as uow:
            rows = uow.repo.rows("locations", {"active": 1}, limit=100000)
            useful = {row["id"] for row in rows if row["initialized_at"] is not None}
            parents = {row["id"]: row["parent_id"] for row in rows}
            for key in list(useful):
                parent = parents.get(key)
                while parent and parent not in useful:
                    useful.add(parent)
                    parent = parents.get(parent)
            return [row for row in rows if row["id"] in useful]

    def contacts(self) -> list[Record]:
        with self.uow() as uow:
            return uow.repo.phone_proposals()

    def setting(self, key: str) -> str:
        with self.uow() as uow:
            return uow.repo.setting(key)
