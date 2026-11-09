import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.domain.entities import Conflict
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_guide.main import create_app

from .test_reviews2 import RESULT, prepared, state
from .test_web import login


def test_search_preserves_existing_review_votes_and_catalogue(database):
    from involta_guide.catalogue.service import CityContent

    settings, engine, city, review, _, analysis, worker, provider, _ = prepared(database)
    try:
        worker.once()
        analysis.approve(review, str(state(engine, review)["job_id"]), "admin")
        with engine.connect() as conn:
            before = [
                dict(row)
                for row in conn.execute(
                    text("SELECT * FROM restaurants ORDER BY number")
                ).mappings()
            ]
            votes = conn.execute(text("SELECT count(*) FROM review_restaurant_votes")).scalar_one()
            review_before = dict(
                conn.execute(text("SELECT * FROM reviews WHERE id=:id"), {"id": review})
                .mappings()
                .one()
            )

        def uow():
            return PostgresUow(engine)

        content = CityContent(uow)
        prepared_context = content.prepare(city["id"], "discover")
        from uuid import uuid4

        content.submit(
            city["id"],
            "discover",
            str(uuid4()),
            prepared_context["expected"],
            {"restaurants": [], "warnings": []},
        )
        with engine.connect() as conn:
            assert [
                dict(row)
                for row in conn.execute(
                    text("SELECT * FROM restaurants ORDER BY number")
                ).mappings()
            ] == before
            assert (
                conn.execute(text("SELECT count(*) FROM review_restaurant_votes")).scalar_one()
                == votes
            )
            assert (
                dict(
                    conn.execute(text("SELECT * FROM reviews WHERE id=:id"), {"id": review})
                    .mappings()
                    .one()
                )
                == review_before
            )
    finally:
        provider.close()


@pytest.mark.parametrize("category", ["humor", "bugs", "other"])
def test_categories_are_visible_without_approval_and_return_to_new_on_text_change(
    database, category
):
    settings, engine, city, review, _, _, worker, provider, _ = prepared(
        database, {**RESULT, "category": category}
    )
    try:
        worker.once()
        assert state(engine, review)["approved_at"] is None
        with TestClient(create_app(settings)) as client:
            login(client)
            with engine.connect() as conn:
                number = conn.execute(
                    text("SELECT number FROM reviews WHERE id=:id"), {"id": review}
                ).scalar_one()
            marker = f'id="review-{number}"'
            assert marker not in client.get("/guide/admin/reviews-2").text
            page = client.get("/guide/admin/reviews-2/" + category)
            assert marker in page.text and "/approve" not in page.text
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE reviews SET transcript='Другой текст' WHERE id=:id"),
                    {"id": review},
                )
            assert marker in client.get("/guide/admin/reviews-2").text
            assert marker not in client.get("/guide/admin/reviews-2/" + category).text
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM review_restaurant_votes")).scalar_one() == 0
            )
    finally:
        provider.close()


def test_rebuild_route_preserves_page_and_destructive_route_is_gone(database):
    settings, _, city, _, _, _, _, provider, _ = prepared(database)
    try:
        with TestClient(create_app(settings)) as client:
            base = "/guide/admin/locations/" + city["slug"]
            assert client.post(base + "/content/discover").status_code == 401
            login(client)
            assert client.post(base + "/content/discover").status_code == 403
            import re

            page = client.get(base)
            token = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
            response = client.post(
                base + "/content/discover",
                data={"_csrf": token},
                headers={"origin": "http://testserver"},
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert "/logs/" + city["slug"] + "/" in response.headers["location"]
            assert client.post(base + "/reset-and-fill", data={"_csrf": token}).status_code in (
                404,
                405,
            )
    finally:
        provider.close()


def test_server_search_uses_discovery_schema_and_retains_paid_response(database):
    import json

    import httpx

    from involta_guide.catalogue.service import CityContent
    from involta_guide.infrastructure.jobs.worker import Worker

    from .test_background_generation import configured, envelope

    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        result = envelope()
        result["id"] = "resp_split_discovery"
        result["output"] = [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": json.dumps({"restaurants": [], "warnings": []})}
                ],
            }
        ]
        return httpx.Response(200, json=result)

    _, _, queries, city, jobs, queue, provider, legacy = configured(database, handler)
    try:
        with jobs.uow() as work:
            work.repo.change(
                "jobs",
                legacy["id"],
                {"status": "cancelled", "lease_owner": None, "lease_until": None},
            )
        identity = CityContent(jobs.uow).enqueue(city["id"], "discover")
        assert Worker(queue, jobs).once()
        assert queries.get("jobs", identity)["status"] == "done"
        assert requests[0]["text"]["format"]["name"] == "content_discover"
        assert "intro" not in requests[0]["text"]["format"]["schema"]["properties"]
        assert len(requests) == 1
    finally:
        provider.close()


def test_ambiguous_catalogue_match_never_guesses():
    from involta_guide.application.services.editorial import match_restaurant

    catalogue = [{"name": "Огонёк", "maps_url": None}, {"name": "Огонек", "maps_url": None}]
    with pytest.raises(Conflict):
        match_restaurant(catalogue, {"name": "ОГОНЕК"})


def test_same_name_with_different_map_identity_is_not_merged():
    from involta_guide.application.services.editorial import match_restaurant

    assert (
        match_restaurant(
            [{"name": "Кофейня", "maps_url": "https://yandex.ru/maps/org/cafe/123/"}],
            {"name": "Кофейня", "mapsUrl": "https://yandex.ru/maps/org/cafe/456/"},
        )
        is None
    )
