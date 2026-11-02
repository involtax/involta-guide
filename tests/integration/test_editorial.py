from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from involta_guide.application.services.editorial import EditorialService
from involta_guide.application.services.locations import LocationService, Queries
from involta_guide.domain.entities import Conflict, RuleViolation
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow

CARD = {
    "name": "Тестовый ресторан",
    "cuisine": "Русская",
    "description": "Описание",
    "mapsUrl": "https://yandex.ru/maps/org/test/123/",
    "phone": "+74951234567",
    "sources": [],
}
CONTENT = {"intro": "Подборка", "restaurants": [CARD]}


def services(database):
    _, engine = database

    def factory():
        return PostgresUow(engine)

    return EditorialService(factory), Queries(factory), factory


def test_draft_publish_restore_provenance_and_conflicts(database):
    editor, queries, uow = services(database)
    location = queries.directory(query="Иваново")["rows"][0]
    key = location["id"]
    first = editor.save(key, CONTENT, None)
    assert queries.get("locations", key)["published_version"] is None
    editor.publish(key, first, None)
    second = editor.save(key, {**CONTENT, "intro": "Новая редакция"}, first)
    assert queries.public(location["slug"])["content"]["intro"] == "Подборка"
    with pytest.raises(Conflict):
        editor.save(key, CONTENT, first)
    editor.publish(key, second, first)
    with pytest.raises(Conflict):
        editor.publish(key, first, first)
    restored = editor.restore(key, first, second)
    assert queries.get("locations", key)["published_version"] == second
    editor.publish(key, restored, second)
    assert (
        queries.public(location["slug"])["content"] == queries.get("versions", restored)["content"]
    )
    empty = editor.save(key, {"intro": "", "restaurants": []}, restored)
    with pytest.raises(RuleViolation):
        editor.publish(key, empty, restored)


def test_parallel_edit_one_wins_and_no_lost_updates(database):
    editor, queries, _ = services(database)
    key = queries.directory(query="Иваново")["rows"][0]["id"]

    def attempt(name):
        try:
            return editor.save(key, {**CONTENT, "intro": name}, None)
        except Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["one", "two"]))
    assert results.count("conflict") == 1
    assert len(queries.rows("versions", {"location_id": key})) == 1


def test_shared_restaurants_do_not_leak_drafts_or_allow_foreign_ids(database):
    editor, queries, uow = services(database)
    first = queries.directory(query="Иваново")["rows"][0]
    second = queries.directory(query="Шуя")["rows"][0]
    version = editor.save(first["id"], CONTENT, None)
    editor.publish(first["id"], version, None)
    card = queries.get("versions", version)["content"]["restaurants"][0]
    with pytest.raises(RuleViolation):
        editor.save(second["id"], {"intro": "", "restaurants": [card]}, None)
    LocationService(uow).attach(card["id"], second["id"])
    copy = editor.save(second["id"], {"intro": "", "restaurants": [card]}, None)
    editor.publish(second["id"], copy, None)
    new = {**card, "description": "Секретный черновик"}
    editor.save(first["id"], {"intro": "", "restaurants": [new]}, version)
    assert queries.public(second["slug"])["content"]["restaurants"][0]["description"] == "Описание"
    assert queries.get("restaurants", card["id"])["location_id"] == first["id"]


def test_proposal_atomic_and_only_once(database):
    editor, queries, uow = services(database)
    key = queries.directory(query="Иваново")["rows"][0]["id"]
    version = editor.save(key, CONTENT, None)
    card = queries.get("versions", version)["content"]["restaurants"][0]
    review_id, proposal_id = str(uuid4()), str(uuid4())
    with uow() as work:
        work.repo.add(
            "reviews",
            {
                "id": review_id,
                "location_id": key,
                "visitor": "v",
                "audio": "test.wav",
                "mime": "audio/wav",
                "hash": "h",
                "ip_hash": "ip",
                "device": "test",
            },
        )
        work.repo.add(
            "proposals",
            {
                "id": proposal_id,
                "location_id": key,
                "review_id": review_id,
                "kind": "edit",
                "payload": {"restaurantId": card["id"]},
            },
        )
    with pytest.raises(RuleViolation):
        editor.proposal(proposal_id, {**card, "description": "Правка"})
    editor.moderate(review_id, "approved", "ok")
    new = editor.proposal(proposal_id, {**card, "description": "Правка"})
    assert queries.get("versions", new)["content"]["restaurants"][0]["description"] == "Правка"
    with pytest.raises(Conflict):
        editor.proposal(proposal_id, card)
    with uow() as work:
        assert work.repo.rows("version_reviews", {"version_id": new})[0]["review_id"] == review_id


def test_location_alias_cycles_visibility_and_shared_direction(database):
    from sqlalchemy.exc import IntegrityError

    from involta_guide.application.commands.schemas import LocationInput

    editor, queries, uow = services(database)
    service = LocationService(uow)
    moscow = queries.directory(query="Москва")["rows"][0]
    assert queries.directory(query="Капотня", public_only=True, status="active")["total"] == 0
    assert queries.directory(query="Капотня")["total"] == 1
    assert (
        queries.directory(query="патрики", public_only=True)["rows"][0]["name"]
        == "Патриаршие пруды"
    )
    assert len(queries.public("moskva")["children"]) == 20
    data = LocationInput(
        name="Новая бухта",
        region="Тест",
        slug="new-bay",
        in_phrase="в Новой бухте",
        kind="resort",
        parent_id=moscow["id"],
    ).model_dump(mode="json")
    key = service.save(None, data)
    data["slug"] = "new-bay-canonical"
    service.save(key, data)
    assert queries.public("new-bay")["redirect"]
    changed = LocationInput.model_validate(
        {k: v for k, v in moscow.items() if k in LocationInput.model_fields}
    ).model_dump(mode="json")
    changed["parent_id"] = key
    with pytest.raises((RuleViolation, IntegrityError)):
        service.save(moscow["id"], changed)
    version = editor.save(key, CONTENT, None)
    editor.publish(key, version, None)
    parent = queries.location(moscow["id"])
    assert any(r["name"] == CARD["name"] for r in parent["available"])
    snapshot = queries.location(key)["content"]["restaurants"][0]
    parent_version = editor.save(
        moscow["id"], {"intro": "Направление", "restaurants": [snapshot]}, None
    )
    editor.publish(moscow["id"], parent_version, None)
    assert queries.get("restaurants", snapshot["id"])["location_id"] == key
    assert len(queries.rows("restaurants")) == 1
