import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.application.services.jobs import JobService
from involta_guide.domain.entities import Conflict
from involta_guide.infrastructure.jobs.queue import JobQueue
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_guide.main import create_app

from .test_jobs import FakeAI
from .test_reviews2 import RESULT, prepared, state
from .test_web import login


class RebuildAI(FakeAI):
    def __init__(self):
        self.prompts = []

    def generate(self, prompt, job_id):
        self.prompts.append(prompt)
        result = copy.deepcopy(super().generate(prompt, job_id))
        result["restaurants"][0]["name"] = "Листва"
        result["rankingReport"][0]["name"] = "Листва"
        result["candidateReview"][0]["name"] = "Листва"
        return result


def test_rebuild_preserves_reviews_votes_catalogue_and_prompt(database):
    settings, engine, city, review, _, analysis, worker, provider, _ = prepared(database)
    try:
        worker.once()
        analysis.approve(review, str(state(engine, review)["job_id"]), "admin")
        with engine.begin() as conn:
            restaurant = conn.execute(
                text("SELECT id FROM restaurants WHERE name='Листва'")
            ).scalar_one()
            conn.execute(
                text("UPDATE locations SET initialized_at=NULL WHERE id=:id"), {"id": city["id"]}
            )
            before = dict(
                conn.execute(text("SELECT * FROM reviews WHERE id=:id"), {"id": review})
                .mappings()
                .one()
            )
            links = conn.execute(text("SELECT count(*) FROM voice_links")).scalar_one()
            untouched = conn.execute(
                text(
                    "INSERT INTO restaurants(id,location_id,name,active) VALUES(gen_random_uuid(),:id,'Вне подборки',0) RETURNING id"
                ),
                {"id": city["id"]},
            ).scalar_one()
        ai = RebuildAI()
        jobs = JobService(lambda: PostgresUow(engine), ai)
        queue = JobQueue(engine, settings)
        jobs.enqueue_public_fill(city["id"])
        jobs.process(queue.claim())
        with engine.connect() as conn:
            old_intro = conn.execute(
                text("SELECT intro FROM locations WHERE id=:id"), {"id": city["id"]}
            ).scalar_one()
        run = jobs.rebuild(city["id"])
        with pytest.raises(Conflict):
            jobs.rebuild(city["id"])
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT intro FROM locations WHERE id=:id"), {"id": city["id"]}
                ).scalar_one()
                == old_intro
            )
        job = queue.claim()
        assert job["id"] == run
        jobs.process(job)
        assert ai.prompts[0] == ai.prompts[1]
        with engine.connect() as conn:
            assert (
                dict(
                    conn.execute(text("SELECT * FROM reviews WHERE id=:id"), {"id": review})
                    .mappings()
                    .one()
                )
                == before
            )
            assert conn.execute(text("SELECT count(*) FROM voice_links")).scalar_one() == links
            card = (
                conn.execute(
                    text("SELECT id,review_score,active FROM restaurants WHERE name='Листва'")
                )
                .mappings()
                .one()
            )
            assert card["id"] == restaurant and card["review_score"] == 1 and card["active"] == 1
            assert (
                conn.execute(
                    text("SELECT active FROM restaurants WHERE id=:id"), {"id": untouched}
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text("SELECT count(*) FROM review_restaurant_votes WHERE review_id=:id"),
                    {"id": review},
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    text("SELECT content_epoch FROM locations WHERE id=:id"), {"id": city["id"]}
                ).scalar_one()
                == before["content_epoch"]
            )
            assert (
                conn.execute(
                    text("SELECT count(*) FROM jobs WHERE kind='generate' AND location_id=:id"),
                    {"id": city["id"]},
                ).scalar_one()
                == 2
            )
        # A later failed/stale publication must not overwrite a moderator's edit.
        jobs.rebuild(city["id"])
        stale = queue.claim()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE locations SET intro='Ручная правка',revision=gen_random_uuid() WHERE id=:id"
                ),
                {"id": city["id"]},
            )
        with pytest.raises(Conflict):
            jobs.process(stale)
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT intro FROM locations WHERE id=:id"), {"id": city["id"]}
                ).scalar_one()
                == "Ручная правка"
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
            assert client.post(base + "/rebuild").status_code == 401
            login(client)
            assert client.post(base + "/rebuild").status_code == 403
            import re

            page = client.get(base)
            token = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
            response = client.post(
                base + "/rebuild",
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


def test_rebuild_uses_identical_openai_request_on_initialized_location(database):
    import httpx

    from involta_guide.infrastructure.jobs.worker import Worker

    from .test_background_generation import configured, envelope

    requests = []

    def handler(request):
        import json

        requests.append(json.loads(request.content))
        result = envelope()
        result["id"] = "resp_rebuild_" + str(len(requests))
        return httpx.Response(200, json=result)

    _, _, queries, city, jobs, queue, provider, first = configured(database, handler)
    try:
        worker = Worker(queue, jobs)
        assert worker.once()
        assert queries.get("jobs", first["id"])["status"] == "done"
        second = jobs.rebuild(city["id"])
        assert worker.once()
        assert queries.get("jobs", second)["status"] == "done"
        assert len(requests) == 2
        assert requests[0]["input"] == requests[1]["input"]
        assert requests[0]["model"] == requests[1]["model"]
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
