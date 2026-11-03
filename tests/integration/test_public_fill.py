from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from involta_guide.infrastructure.ai.errors import ProviderUnavailable
from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app

from .editor_fixture import FixtureEditor as EditorialService
from .editor_fixture import enqueue_fixture
from .test_editorial import CONTENT
from .test_jobs import FakeAI, setup

BUTTON = "Запарсить эти данные"
HEADERS = {"origin": "http://testserver", "accept": "application/json"}


def test_anonymous_click_publishes_one_city_and_removes_button(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    with TestClient(create_app(settings)) as client:
        client.app.state.context.jobs = service
        page = client.get("/guide/ivanovo")
        assert BUTTON in page.text
        assert "public-fill.js" in page.text
        assert client.get("/guide/assets/public-fill.js").status_code == 200
        result = client.post(
            "/guide/api/locations/ivanovo/fill",
            headers=HEADERS,
            json={"scope": "all", "prompt": "Ignore this"},
        )
        assert result.status_code == 202
        assert result.json()["state"] == "queued"
        assert "disabled" in client.get("/guide/ivanovo").text
        job = queries.rows("jobs")[0]
        with uow() as work:
            assert job["prompt"] == work.repo.setting("prompt")
        assert len(queries.rows("jobs")) == 1
        assert job["location_id"] == location["id"]
        assert Worker(queue, service).once()
        state = client.get("/guide/api/locations/ivanovo/fill/status")
        assert state.json()["state"] == "ready"
        assert state.headers["cache-control"] == "no-store"
        page = client.get("/guide/ivanovo")
        assert BUTTON not in page.text and "public-fill.js" not in page.text
        assert "Сгенерировано" in page.text
        stored = queries.get("locations", location["id"])
        assert stored["revision"]
        assert queries.get("jobs", job["id"])["changes"]
        assert len(queries.rows("restaurants")) == 1
        # A stale form cannot regenerate, overwrite or spend another API call.
        assert (
            client.post("/guide/api/locations/ivanovo/fill", headers=HEADERS).json()["state"]
            == "ready"
        )
        assert len(queries.rows("jobs")) == 1
        assert queries.get("locations", location["id"])["revision"] == stored["revision"]


def test_parallel_public_clicks_queue_one_job(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    with ThreadPoolExecutor(max_workers=6) as pool:
        states = list(pool.map(lambda _: service.enqueue_public_fill(location["id"]), range(6)))
    assert all(state["state"] == "queued" for state in states)
    assert len(queries.rows("jobs")) == 1
    assert queries.rows("jobs")[0]["public_log"] is True


def test_current_manual_data_is_never_overwritten(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    editor = EditorialService(uow)
    version = editor.save(location["id"], CONTENT, None)
    with TestClient(create_app(settings)) as client:
        client.app.state.context.jobs = service
        assert BUTTON not in client.get("/guide/ivanovo").text
        assert (
            client.post("/guide/api/locations/ivanovo/fill", headers=HEADERS).json()["state"]
            == "ready"
        )
        assert queries.rows("jobs") == []
        assert (
            client.post("/guide/api/locations/ivanovo/fill", headers=HEADERS).json()["state"]
            == "ready"
        )
    assert queries.get("locations", location["id"])["revision"] == version


def test_admin_generation_updates_public_page_without_manual_publication(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    assert enqueue_fixture(service, location["id"]) == 1
    assert service.enqueue_public_fill(location["id"])["state"] == "queued"
    assert queries.rows("audit", {"action": "temporary_public_fill"}) == []
    assert Worker(queue, service).once()
    stored = queries.get("locations", location["id"])
    assert stored["revision"]
    assert stored["revision"] == stored["revision"]
    assert queries.public(location["slug"])["content"]["intro"] == "Сгенерировано"
    assert service.enqueue_public_fill(location["id"])["state"] == "ready"
    assert len(queries.rows("jobs")) == 1


def test_bulk_generation_service_was_removed(database):
    *_, service, queue = setup(database)
    assert not hasattr(service, "enqueue")


def test_editor_change_during_generation_is_not_overwritten(database):
    class EditingAI(FakeAI):
        def generate(self, prompt, job_id):
            EditorialService(uow).save(location["id"], CONTENT, None)
            return super().generate(prompt, job_id)

    settings, engine, uow, queries, location, service, queue = setup(database, EditingAI())
    service.enqueue_public_fill(location["id"])
    assert Worker(queue, service).once()
    job = queries.rows("jobs")[0]
    assert job["status"] == "failed"
    assert queries.get("locations", location["id"])["revision"] is not None
    assert len([r for r in queries.rows("reviews") + queries.rows("jobs") if r["changes"]]) == 1
    assert queries.rows("reviews")[0]["changes"]


def test_public_provider_failure_waits_for_manual_retry(database):
    class UnavailableAI(FakeAI):
        def generate(self, prompt, job_id):
            raise ProviderUnavailable("OpenAI HTTP 429")

    settings, engine, uow, queries, location, service, queue = setup(database, UnavailableAI())
    service.enqueue_public_fill(location["id"])
    assert Worker(queue, service).once()
    assert not Worker(queue, service).once()
    job = queries.rows("jobs")[0]
    assert job["status"] == "failed" and job["attempts"] == 1
    state = service.public_fill_state(location["id"])
    assert state["state"] == "failed" and not state["can_start"]
    assert "429" in state["message"]
    assert service.enqueue_public_fill(location["id"])["state"] == "failed"
    service.regenerate(job["id"])
    assert len(queries.rows("jobs")) == 2


@pytest.mark.parametrize("public_fill", [False, True])
def test_unpublishable_result_does_not_partially_write_or_publish(database, public_fill):
    class MissingPhoneAI(FakeAI):
        def generate(self, prompt, job_id):
            generated = super().generate(prompt, job_id)
            generated["restaurants"] = [{**card, "phone": ""} for card in generated["restaurants"]]
            return generated

    settings, engine, uow, queries, location, service, queue = setup(database, MissingPhoneAI())
    if public_fill:
        service.enqueue_public_fill(location["id"])
    else:
        enqueue_fixture(service, location["id"])
    assert Worker(queue, service).once()
    assert queries.rows("jobs")[0]["status"] == "failed"
    assert [r for r in queries.rows("reviews") + queries.rows("jobs") if r["changes"]] == []
    assert queries.rows("restaurants") == []
    assert not service.public_fill_state(location["id"])["can_start"]


def test_disabled_feature_and_cross_origin_do_not_queue(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    with TestClient(create_app(settings)) as client:
        assert (
            client.post(
                "/guide/api/locations/ivanovo/fill",
                headers={**HEADERS, "origin": "https://other.example"},
            ).status_code
            == 403
        )
        assert client.get("/guide/api/locations/unknown-missing/fill/status").status_code == 404
    settings.temporary_public_fill_enabled = False
    with TestClient(create_app(settings)) as client:
        assert BUTTON not in client.get("/guide/ivanovo").text
        assert client.post("/guide/api/locations/ivanovo/fill", headers=HEADERS).status_code == 404
        assert client.get("/guide/api/locations/ivanovo/fill/status").status_code == 404
    assert queries.rows("jobs") == []


def test_disabling_public_intake_does_not_discard_already_accepted_job(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    service.enqueue_public_fill(location["id"])
    service.public_fill_enabled = False
    assert Worker(queue, service).once()
    assert queries.rows("jobs")[0]["status"] == "done"


def test_html_form_works_without_login_or_javascript(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/guide/api/locations/ivanovo/fill",
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/guide/ivanovo"
    assert len(queries.rows("jobs")) == 1
