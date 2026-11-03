from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from involta_guide.application.services.locations import LocationService, Queries
from involta_guide.domain.entities import Conflict, RuleViolation
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow

from .editor_fixture import FixtureEditor as EditorialService

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


def test_current_state_diffs_and_conflicts(database):
    editor, queries, uow = services(database)
    location = queries.directory(query="Иваново")["rows"][0]
    key = location["id"]
    first = editor.save(key, CONTENT, None)
    content = queries.location(key)["content"]
    second = editor.save(key, {**content, "intro": "Новая редакция"}, first)
    assert queries.get("locations", key)["revision"] == second
    assert queries.public(location["slug"])["content"]["intro"] == "Новая редакция"
    change = queries.rows("reviews")[0]["changes"][0]
    assert change["diff"] == [
        {"path": ["intro"], "before": CONTENT["intro"], "after": "Новая редакция"}
    ]
    with pytest.raises(Conflict):
        editor.save(key, CONTENT, first)
    assert len(queries.rows("reviews")) == 2  # Failed change rolled back with its source.
    empty = editor.save(key, {"intro": "", "restaurants": []}, second)
    assert queries.get("locations", key)["revision"] == empty
    assert queries.public(location["slug"])["content"]["restaurants"] == []


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
    assert len([r for r in queries.rows("reviews", {"location_id": key}) if r["changes"]]) == 1


def test_shared_restaurants_use_current_data_and_reject_foreign_ids(database):
    editor, queries, uow = services(database)
    first = queries.directory(query="Иваново")["rows"][0]
    second = queries.directory(query="Шуя")["rows"][0]
    version = editor.save(first["id"], CONTENT, None)
    card = queries.location(first["id"])["content"]["restaurants"][0]
    with pytest.raises(RuleViolation):
        editor.save(second["id"], {"intro": "", "restaurants": [card]}, None)
    LocationService(uow).attach(card["id"], second["id"])
    editor.save(second["id"], {"intro": "", "restaurants": [card]}, None)
    new = {**card, "description": "Обновлённое описание"}
    editor.save(first["id"], {"intro": "", "restaurants": [new]}, version)
    assert (
        queries.public(second["slug"])["content"]["restaurants"][0]["description"]
        == "Обновлённое описание"
    )
    assert queries.get("restaurants", card["id"])["location_id"] == first["id"]


def test_proposal_atomic_and_only_once(database):
    editor, queries, uow = services(database)
    key = queries.directory(query="Иваново")["rows"][0]["id"]
    editor.save(key, CONTENT, None)
    card = queries.location(key)["content"]["restaurants"][0]
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
    editor.proposal(proposal_id, {**card, "description": "Правка"})
    assert queries.location(key)["content"]["restaurants"][0]["description"] == "Правка"
    with pytest.raises(Conflict):
        editor.proposal(proposal_id, card)
    with uow() as work:
        assert work.repo.get("reviews", review_id)["changes"][0]["diff"] == [
            {
                "path": ["restaurants", card["id"], "description"],
                "before": "Описание",
                "after": "Правка",
            }
        ]


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
    assert queries.public("new-bay") is None
    changed = LocationInput.model_validate(
        {k: v for k, v in moscow.items() if k in LocationInput.model_fields}
    ).model_dump(mode="json")
    changed["parent_id"] = key
    with pytest.raises((RuleViolation, IntegrityError)):
        service.save(moscow["id"], changed)
    editor.save(key, CONTENT, None)
    parent = queries.location(moscow["id"])
    assert any(r["name"] == CARD["name"] for r in parent["available"])
    snapshot = queries.location(key)["content"]["restaurants"][0]
    editor.save(moscow["id"], {"intro": "Направление", "restaurants": [snapshot]}, None)
    assert queries.get("restaurants", snapshot["id"])["location_id"] == key
    assert len(queries.rows("restaurants")) == 1


def test_page_does_not_depend_on_its_source_log(database):
    from sqlalchemy import text

    editor, queries, factory = services(database)
    city = queries.directory(query="Иваново")["rows"][0]
    editor.save(city["id"], CONTENT, None)
    before = queries.public(city["slug"])["content"]
    with factory() as work:
        work.session.execute(text("DELETE FROM reviews WHERE visitor='fixture-editor'"))
    assert queries.rows("reviews") == []
    assert queries.public(city["slug"])["content"] == before


def test_application_requires_one_matching_source(database):
    from involta_guide.application.services.editorial import save

    _, queries, factory = services(database)
    city = queries.directory(query="Иваново")["rows"][0]
    with pytest.raises(RuleViolation), factory() as work:
        save(work.repo, city["id"], CONTENT, None)
    assert queries.get("locations", city["id"])["revision"] is None
