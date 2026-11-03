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

    def save(self, key: str | None, values: Record) -> str:
        with self.uow() as uow:
            repo = uow.repo
            target = key or str(uuid4())
            data = values.copy()
            related = data.pop("related_ids", [])
            if data["slug"] in {"new", "export"}:
                raise RuleViolation("Этот URL зарезервирован для служебного раздела")
            if any(row["id"] != target for row in repo.rows("locations", {"slug": data["slug"]})):
                raise RuleViolation("Этот URL уже занят другой локацией")
            if key:
                require(repo, "locations", key, lock=True)
            if data["parent_id"] == target or target in related:
                raise RuleViolation("Локация не может ссылаться на себя")
            if data["parent_id"]:
                parent = require(repo, "locations", data["parent_id"])
                if data["active"] and not parent["active"]:
                    raise RuleViolation("Сначала включите родительскую локацию")
                if target in {row["id"] for row in repo.tree(data["parent_id"], ancestors=True)}:
                    raise RuleViolation("Нельзя создавать цикл")
            children = [r for r in repo.tree(target)[1:] if r["active"]] if key else []
            if not data["active"] and children:
                raise RuleViolation("Сначала отключите дочерние локации")
            data["is_hub"] = bool(data["is_hub"] or children)
            if key:
                repo.change("locations", target, data)
            else:
                repo.add("locations", {"id": target, **data})
            if data["parent_id"]:
                repo.change("locations", data["parent_id"], {"is_hub": True})
            repo.replace_relations(target, related)
            repo.audit("location", target, json.dumps(values, ensure_ascii=False))
            return target

    def save_intro(self, key: str, text: str, expected: str | None, actor: str) -> bool:
        if len(text) > 30000 or "\x00" in text:
            raise RuleViolation("Вступление: до 30000 символов, без нулевого символа")
        with self.uow() as uow:
            repo = uow.repo
            location = require(repo, "locations", key, lock=True)
            if location["revision"] != expected:
                raise Conflict("Город изменён другим действием. Сравните тексты и сохраните снова.")
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
                        "fields": [{"field": "intro", "before": location["intro"], "after": text}],
                    },
                    ensure_ascii=False,
                ),
            )
            return True

    def attach(self, restaurant_id: str, location_id: str) -> None:
        with self.uow() as uow:
            require(uow.repo, "restaurants", restaurant_id)
            require(uow.repo, "locations", location_id)
            uow.repo.link(
                "restaurant_locations", {"restaurant_id": restaurant_id, "location_id": location_id}
            )
            uow.repo.audit("restaurant_location", location_id, restaurant_id)


class Queries:
    def __init__(self, uow: UowFactory) -> None:
        self.uow = uow

    def health(self) -> None:
        with self.uow() as uow:
            uow.repo.health()

    def directory(self, **options: Any) -> Record:
        with self.uow() as uow:
            return uow.repo.search_locations(**options)

    def overview(self) -> Record:
        with self.uow() as uow:
            return uow.repo.summary()

    def get(self, table: str, key: str) -> Record:
        with self.uow() as uow:
            return require(uow.repo, table, key)

    def rows(self, table: str, filters: Record | None = None, *, limit: int = 200) -> list[Record]:
        with self.uow() as uow:
            return uow.repo.rows(table, filters, limit=limit)

    def location(self, key: str) -> Record:
        from .editorial import current_content

        with self.uow() as uow:
            repo = uow.repo
            location = require(repo, "locations", key)
            return {
                "location": location,
                "content": current_content(repo, location),
                "available": repo.available(key),
                "related_ids": [row["id"] for row in repo.related(key)],
                "parents": repo.tree(key, ancestors=True),
            }

    def admin_location(self, slug: str) -> Record:
        with self.uow() as uow:
            rows = uow.repo.rows("locations", {"slug": slug}, limit=1)
            if not rows:
                raise NotFound("Локация не найдена")
            key = rows[0]["id"]
            events = uow.repo.rows("audit", {"action": "manual_intro", "target": key}, limit=1000)
            for event in events:
                event["data"] = json.loads(event["detail"])
        return {**self.location(key), "intro_events": events}

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

    def restaurant_browser(self, query: str = "", location_id: str | None = None) -> Record:
        from involta_guide.domain.rules import FACTOR_LABELS, FACTORS, score

        with self.uow() as uow:
            repo = uow.repo
            cities = repo.restaurant_cities(query)
            selected = next((c for c in cities if c["id"] == location_id), None)
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
            location = require(repo, "locations", selected["id"])
            content = current_content(repo, location)
            report = content.get("rankingReport") or []
            result["generation_quality"] = content.get("qualityReport")
            result["candidate_review"] = content.get("candidateReview")

            def normalize(name: str) -> str:
                return " ".join(name.casefold().split())

            for card in cards:
                candidate = next(
                    (
                        r
                        for r in report
                        if normalize(r["name"]) == normalize(card["name"])
                        and (not r.get("mapsUrl") or r["mapsUrl"] == card["maps_url"])
                    ),
                    None,
                )
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
                card["ranking"] = candidate
                card["score"] = score(candidate["scores"]) if candidate else None
            cards.sort(
                key=lambda c: (
                    c["score"] is None,
                    -(c["score"] if c["score"] is not None else 0),
                    c["review_scores"]["rating"] is None,
                    -(c["review_scores"]["rating"] or 0),
                    -int(c["alex_star"]),
                    normalize(c["name"]),
                    c["id"],
                )
            )
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
            useful = {r["id"] for r in tree if r["page_data"].get("restaurants")}
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
            related = [
                r
                for r in repo.related(location["id"])
                if r["active"]
                and (
                    r["listed"]
                    or any(item["page_data"].get("restaurants") for item in repo.tree(r["id"]))
                )
            ]
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
            useful = {row["id"] for row in rows if row["page_data"].get("restaurants")}
            parents = {row["id"]: row["parent_id"] for row in rows}
            for key in list(useful):
                parent = parents.get(key)
                while parent and parent not in useful:
                    useful.add(parent)
                    parent = parents.get(parent)
            return [row for row in rows if row["id"] in useful]

    def review(self, key: str) -> Record:
        with self.uow() as uow:
            repo = uow.repo
            review = require(repo, "reviews", key)
            proposals = repo.rows("proposals", {"review_id": key})
            return {
                "review": review,
                "proposals": proposals,
                "signals": repo.signals(review),
                "location": require(repo, "locations", review["location_id"]),
            }

    def visitor(self, visitor: str, location_id: str) -> list[Record]:
        with self.uow() as uow:
            repo = uow.repo
            require(repo, "locations", location_id)
            return [
                {key: review[key] for key in ("id", "moderation", "status", "fraud", "created_at")}
                | {
                    "proposals": [
                        {"status": p["status"], "kind": p["kind"]}
                        for p in repo.rows("proposals", {"review_id": review["id"]})
                    ],
                    "published": bool(review["changes"])
                    and review["moderation"] == "approved"
                    and not review["fraud"],
                }
                for review in repo.rows(
                    "reviews", {"visitor": visitor, "location_id": location_id}, limit=30
                )
            ]

    def contacts(self) -> list[Record]:
        with self.uow() as uow:
            return uow.repo.phone_proposals()

    def setting(self, key: str) -> str:
        with self.uow() as uow:
            return uow.repo.setting(key)
