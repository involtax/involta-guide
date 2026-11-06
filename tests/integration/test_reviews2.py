import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from involta_guide.application.services.jobs import JobService
from involta_guide.domain.entities import Conflict
from involta_guide.infrastructure.ai.openai import OpenAIProvider
from involta_guide.infrastructure.jobs.queue import JobQueue
from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_guide.main import create_app
from involta_guide.review_analysis.service import ReviewAnalysis

from .test_web import login
from .voice_fixture import imported

SOURCE = "Меня зовут Иван. Добавьте ресторан Листва, вкусно там."
RESULT = {
    "category": "ok",
    "author_name": "Иван",
    "edited_text": "Добавьте ресторан Листва, вкусно там.",
    "mentions": [
        {
            "name": "Листва",
            "restaurant_id": None,
            "sentiment": 1,
            "suggested": True,
            "evidence": "Добавьте ресторан Листва",
        }
    ],
    "reason": "Предложение ресторана",
}


def prepared(database, result=None):
    settings, engine, _, _, city, review, event = imported(database)
    settings = settings.model_copy(update={"openai_api_key": SecretStr("test-no-real-key")})
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE reviews SET transcript=:source WHERE id=:id"),
            {"source": SOURCE, "id": review},
        )
        # Reviews are analyzed after primary filling, too.
        conn.execute(
            text("UPDATE locations SET initialized_at=now() WHERE id=:id"), {"id": city["id"]}
        )
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_reviews2_" + str(len(calls)),
                "status": "completed",
                "usage": {"input_tokens": 100, "output_tokens": 100},
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(result or RESULT)}],
                    }
                ],
            },
        )

    provider = OpenAIProvider(settings, engine)
    provider.client.close()
    provider.client = httpx.Client(transport=httpx.MockTransport(handler))
    service = ReviewAnalysis(engine, settings)
    worker = Worker(JobQueue(engine, settings), JobService(lambda: PostgresUow(engine), provider))
    return settings, engine, city, review, event, service, worker, provider, calls


def state(engine, review):
    with engine.connect() as conn:
        return dict(
            conn.execute(text("SELECT * FROM review_analyses WHERE review_id=:id"), {"id": review})
            .mappings()
            .one()
        )


def test_automatic_analysis_then_atomic_approval_new_restaurant_and_delete(database):
    settings, engine, city, review, _, service, worker, provider, calls = prepared(database)
    try:
        assert worker.once()
        a = state(engine, review)
        assert a["result"] == RESULT and a["approved_at"] is None
        assert len(calls) == 1 and "tools" not in calls[0]
        assert "192." not in calls[0]["input"][1]["content"]
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM restaurants WHERE name='Листва'")).scalar()
                == 0
            )
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda _: service.approve(review, str(a["job_id"]), "admin"), range(2)))
        with engine.connect() as conn:
            restaurant = (
                conn.execute(text("SELECT * FROM restaurants WHERE name='Листва'")).mappings().one()
            )
            assert restaurant["active"] == 0 and restaurant["review_score"] == 1
            assert restaurant["phone"] is None and restaurant["ranking"] is None
            r = (
                conn.execute(text("SELECT * FROM reviews WHERE id=:id"), {"id": review})
                .mappings()
                .one()
            )
            assert (
                r["author_name"] == "Иван"
                and r["transcript"] == SOURCE
                and r["edited_text"] == RESULT["edited_text"]
            )
        assert state(engine, review)["result"]["mentions"][0]["restaurant_id"] == str(
            restaurant["id"]
        )
        assert service.schedule() == 0
        service.delete(review, "admin")
        service.delete(review, "admin")
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT review_score FROM restaurants WHERE name='Листва'")
                ).scalar()
                == 0
            )
    finally:
        provider.close()


def test_same_new_restaurant_from_two_reviews_is_one_catalogue_row(database):
    _, engine, _, review, _, service, worker, provider, _ = prepared(database)
    try:
        worker.once()
        service.approve(review, str(state(engine, review)["job_id"]), "admin")
        second = str(uuid4())
        with engine.begin() as conn:
            conn.execute(
                text("""INSERT INTO reviews(id,location_id,content_epoch,visitor,audio,mime,hash,ip,user_agent,transcript,status)
            SELECT :new,location_id,content_epoch,visitor,audio,mime,hash,ip,user_agent,transcript,status FROM reviews WHERE id=:id"""),
                {"new": second, "id": review},
            )
        worker.once()
        service.approve(second, str(state(engine, second)["job_id"]), "admin")
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT review_score FROM restaurants WHERE name='Листва'")
                ).scalar_one()
                == 2
            )
        service.delete(second, "admin")
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT review_score FROM restaurants WHERE name='Листва'")
                ).scalar_one()
                == 1
            )
    finally:
        provider.close()


def test_changed_source_invalidates_votes_and_stale_approval(database):
    _, engine, _, review, _, service, worker, provider, _ = prepared(database)
    try:
        worker.once()
        old = str(state(engine, review)["job_id"])
        service.approve(review, old, "admin")
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE reviews SET transcript=:source WHERE id=:id"),
                {"source": SOURCE + " Ещё вернусь.", "id": review},
            )
        assert state(engine, review)["approved_at"] is None
        with pytest.raises(Conflict):
            service.approve(review, old, "admin")
        assert service.schedule() == 1
        assert str(state(engine, review)["job_id"]) != old
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT review_score FROM restaurants WHERE name='Листва'")
                ).scalar_one()
                == 0
            )
    finally:
        provider.close()


@pytest.mark.parametrize("category", ["bugs", "other"])
def test_non_restaurant_categories_never_create_restaurants(database, category):
    _, engine, _, review, _, service, worker, provider, _ = prepared(
        database, {**RESULT, "category": category}
    )
    try:
        worker.once()
        service.approve(review, str(state(engine, review)["job_id"]), "admin")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM review_restaurant_votes")).scalar() == 0
            assert (
                conn.execute(text("SELECT count(*) FROM restaurants WHERE name='Листва'")).scalar()
                == 0
            )
    finally:
        provider.close()


def test_admin_routes_csrf_public_log_and_old_delete_path(database):
    settings, engine, city, review, event, service, worker, provider, _ = prepared(database)
    try:
        with TestClient(create_app(settings)) as client:
            base = "/guide/admin/reviews-2"
            assert client.get(base).status_code == 401
            csrf = login(client)
            assert client.get(base).status_code == 200
            # Reading does not schedule a paid job.
            with engine.connect() as conn:
                assert (
                    conn.execute(
                        text("SELECT count(*) FROM jobs WHERE kind='review_analysis'")
                    ).scalar()
                    == 0
                )
            worker.once()
            with engine.connect() as conn:
                number = conn.execute(
                    text("SELECT number FROM reviews WHERE id=:id"), {"id": review}
                ).scalar_one()
            path = f"{base}/{city['slug']}/{number}/approve"
            assert client.post(path).status_code == 403
            response = client.post(
                path,
                data={"_csrf": csrf, "job_id": str(state(engine, review)["job_id"])},
                headers={"origin": "http://testserver"},
            )
            assert response.status_code == 200
            assert "Листва" in client.get(base + "/ok").text
            log = f"/guide/logs/reviews-2/{city['slug']}/{number}"
            with TestClient(create_app(settings)) as public:
                data = public.get(log)
                assert data.status_code == 200 and "Листва" in data.text
                assert event["metadata"]["ip"] not in data.text
                assert "user_agent" not in data.text and "test-no-real-key" not in data.text
                old = f"/guide/admin/reviews/{city['slug']}/{number}/delete"
                assert (
                    client.post(
                        old, data={"_csrf": csrf}, headers={"origin": "http://testserver"}
                    ).status_code
                    == 200
                )
                assert public.get(log).status_code == 404
            with engine.connect() as conn:
                assert (
                    conn.execute(
                        text("SELECT review_score FROM restaurants WHERE name='Листва'")
                    ).scalar()
                    == 0
                )
    finally:
        provider.close()


def test_untrusted_restaurant_id_fails_with_saved_response(database):
    result = {**RESULT, "mentions": [{**RESULT["mentions"][0], "restaurant_id": str(uuid4())}]}
    _, engine, _, review, _, _, worker, provider, _ = prepared(database, result)
    try:
        worker.once()
        a = state(engine, review)
        assert a["result"] is None
        with engine.connect() as conn:
            job = (
                conn.execute(text("SELECT * FROM jobs WHERE id=:id"), {"id": a["job_id"]})
                .mappings()
                .one()
            )
            assert job["status"] == "failed"
            assert (
                conn.execute(
                    text("SELECT response_json FROM generation_requests WHERE job_id=:id"),
                    {"id": a["job_id"]},
                ).scalar()
                is not None
            )
    finally:
        provider.close()


def test_negative_vote_removed_and_deleted_review_cannot_be_approved(database):
    settings, engine, city, review, _, service, worker, provider, _ = prepared(database)
    try:
        restaurant = str(uuid4())
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO restaurants(id,location_id,name,cuisine,description,maps_url,phone,active) VALUES(:id,:loc,'Хикари',NULL,NULL,NULL,NULL,0)"
                ),
                {"id": restaurant, "loc": city["id"]},
            )
            conn.execute(
                text("UPDATE reviews SET transcript='В Хикари не понравилось' WHERE id=:id"),
                {"id": review},
            )
        answer = {
            **RESULT,
            "author_name": None,
            "edited_text": "В Хикари не понравилось",
            "mentions": [
                {
                    "name": "Хикари",
                    "restaurant_id": restaurant,
                    "sentiment": -1,
                    "suggested": False,
                    "evidence": "В Хикари не понравилось",
                }
            ],
        }
        provider.client.close()
        provider.client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "id": "resp_negative",
                        "status": "completed",
                        "output": [
                            {"content": [{"type": "output_text", "text": json.dumps(answer)}]}
                        ],
                        "usage": {},
                    },
                )
            )
        )
        worker.once()
        job = str(state(engine, review)["job_id"])
        service.approve(review, job, "admin")
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT review_score FROM restaurants WHERE id=:id"), {"id": restaurant}
                ).scalar()
                == -1
            )
        service.delete(review, "admin")
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT review_score FROM restaurants WHERE id=:id"), {"id": restaurant}
                ).scalar()
                == 0
            )
        from involta_guide.domain.entities import NotFound

        with pytest.raises(NotFound):
            service.approve(review, job, "admin")
    finally:
        provider.close()


def test_late_provider_response_does_not_restore_deleted_review(database):
    _, engine, _, review, _, service, worker, provider, _ = prepared(database)
    try:

        def response(request):
            service.delete(review, "admin")
            return httpx.Response(
                200,
                json={
                    "id": "resp_deleted",
                    "status": "completed",
                    "output": [{"content": [{"type": "output_text", "text": json.dumps(RESULT)}]}],
                    "usage": {},
                },
            )

        provider.client.close()
        provider.client = httpx.Client(transport=httpx.MockTransport(response))
        worker.once()
        assert state(engine, review)["result"] is None
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT status FROM reviews WHERE id=:id"), {"id": review}
                ).scalar()
                == "deleted"
            )
            assert conn.execute(text("SELECT count(*) FROM review_restaurant_votes")).scalar() == 0
    finally:
        provider.close()


def test_location_reset_cascades_analysis_votes_without_touching_other_catalogue(database):
    _, engine, city, review, _, service, worker, provider, _ = prepared(database)
    try:
        worker.once()
        service.approve(review, str(state(engine, review)["job_id"]), "admin")
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE locations SET reset_pending=true WHERE id=:id"), {"id": city["id"]}
            )
            # Mirrors the review/restaurant part of beta reset after Voice/link cleanup.
            conn.execute(text("DELETE FROM voice_links WHERE review_id=:id"), {"id": review})
            conn.execute(text("DELETE FROM voice_inbox WHERE review_id=:id"), {"id": review})
            conn.execute(
                text(
                    "DELETE FROM generation_requests WHERE job_id IN (SELECT id FROM jobs WHERE review_id=:id)"
                ),
                {"id": review},
            )
            conn.execute(
                text(
                    "DELETE FROM ai_usage WHERE job_id IN (SELECT id FROM jobs WHERE review_id=:id)"
                ),
                {"id": review},
            )
            conn.execute(text("DELETE FROM jobs WHERE review_id=:id"), {"id": review})
            conn.execute(text("DELETE FROM reviews WHERE id=:id"), {"id": review})
            conn.execute(text("DELETE FROM restaurants WHERE location_id=:id"), {"id": city["id"]})
            assert conn.execute(text("SELECT count(*) FROM review_analyses")).scalar() == 0
            assert conn.execute(text("SELECT count(*) FROM review_restaurant_votes")).scalar() == 0
    finally:
        provider.close()


def test_real_reviews2_template_in_browser(database):
    import os
    from pathlib import Path

    from playwright.sync_api import sync_playwright

    settings, engine, city, review, _, _, worker, provider, _ = prepared(database)
    shots = Path("var/screenshots/reviews2")
    shots.mkdir(parents=True, exist_ok=True)
    try:
        worker.once()
        with TestClient(create_app(settings)) as client, sync_playwright() as p:
            login(client)
            browser = p.chromium.launch(
                executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
            )
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def respond(route):
                response = client.request(
                    route.request.method,
                    route.request.url,
                    headers={
                        k: v
                        for k, v in route.request.headers.items()
                        if k
                        in ("hx-request", "hx-history-restore-request", "origin", "content-type")
                    },
                    content=route.request.post_data,
                )
                route.fulfill(
                    status=response.status_code,
                    body=response.content,
                    headers=dict(response.headers),
                )

            page.route("**/*", respond)
            for width in (1440, 768, 390):
                page.set_viewport_size({"width": width, "height": 1000})
                page.goto("http://testserver/guide/admin/reviews-2", wait_until="networkidle")
                assert page.locator(".reviews2-card").count() == 1
                assert page.locator(".reviews2-before").inner_text().endswith(SOURCE)
                assert "добавим по ОК" in page.locator(".reviews2-attributes").inner_text()
                assert page.locator(".reviews2-actions button").all_text_contents() == [
                    "ОК",
                    "Удалить",
                ]
                assert page.locator("input[type=search]").count() == 0
                assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
                page.screenshot(path=str(shots / f"new-{width}.png"), full_page=True)
            page.get_by_role("button", name="ОК", exact=True).click()
            page.get_by_text("Здесь пока нет отзывов.", exact=True).wait_for()
            page.locator(".reviews2-filters a").filter(has_text="ОК").click()
            page.locator(".reviews2-card").wait_for()
            page.screenshot(path=str(shots / "approved-390.png"), full_page=True)
            page.get_by_role("button", name="Удалить", exact=True).click()
            page.get_by_text("Здесь пока нет отзывов.", exact=True).wait_for()
            assert not errors
            browser.close()
    finally:
        provider.close()


def test_empty_edited_text_is_retained_for_audit_but_not_published(database):
    result = {**RESULT, "category": "other", "edited_text": "", "mentions": []}
    _, engine, _, review, _, service, worker, provider, _ = prepared(database, result)
    try:
        worker.once()
        service.approve(review, str(state(engine, review)["job_id"]), "admin")
        with engine.connect() as conn:
            item = (
                conn.execute(
                    text(
                        "SELECT transcript,edited_text,publication_status FROM reviews WHERE id=:id"
                    ),
                    {"id": review},
                )
                .mappings()
                .one()
            )
            assert (
                item["transcript"] == SOURCE
                and item["edited_text"] == ""
                and item["publication_status"] == 0
            )
    finally:
        provider.close()
