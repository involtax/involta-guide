import os
from pathlib import Path

import pytest
from integration.test_jobs import setup
from integration.test_voice_upload_transport import serving
from playwright.sync_api import expect, sync_playwright

from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_waiting_reload_and_committed_restaurants_appear_without_user_refresh(database, width):
    settings, _, _, queries, location, service, queue = setup(database)
    app = create_app(settings)
    app.state.context.jobs = service
    with serving(app) as origin, sync_playwright() as p:
        settings.site_url = origin + settings.base_path
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page(viewport={"width": width, "height": 950})
        errors, submissions = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: submissions.append(request.url) if request.method == "POST" else None,
        )
        page.goto(origin + "/guide/ivanovo")
        expect(page.get_by_role("button", name="Запарсить эти данные")).to_be_visible()
        assert queries.rows("jobs") == []  # Opening a city never starts generation.
        page.get_by_role("button", name="Запарсить эти данные").click()
        expect(page.locator("[data-fill-waiting]")).to_be_visible()
        expect(page.get_by_role("button", name="Запарсить эти данные")).to_be_hidden()
        expect(page.locator("[data-fill-spoiler]")).to_be_visible()
        expect(page.locator(".public-fill-intro .lead")).to_be_hidden()
        expect(page.get_by_text("Подборка в работе", exact=True)).to_have_count(0)
        assert page.locator(".public-fill-spinner circle").count() == 12
        expect(page.locator("[data-fill-message]")).to_have_text(
            "Сканируем город и его окрестности.", timeout=7500
        )
        assert len(queries.rows("jobs")) == 1
        page.reload()  # Return to an already queued job: no grey button or second POST.
        expect(page.locator("[data-fill-waiting]")).to_be_visible()
        expect(page.get_by_role("button", name="Запарсить эти данные")).to_be_hidden()
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        evidence = os.getenv("FILL_SCREENSHOTS_DIR")
        if evidence:
            Path(evidence).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(evidence) / f"waiting-{width}.png"), full_page=True)
        navigations = []
        page.on("framenavigated", lambda frame: navigations.append(frame.url))
        assert Worker(queue, service).once()  # Fake provider; real PostgreSQL commit and HTTP GET.
        assert queries.get("locations", location["id"])["initialized_at"] is not None
        assert len(queries.rows("restaurants")) == 1
        expect(page.get_by_role("heading", name="Стоит зайти", exact=True)).to_be_visible(
            timeout=10000
        )
        expect(page.get_by_role("heading", name="Тестовый ресторан")).to_be_visible()
        expect(page.locator(".city-hero .lead")).to_have_text("Сгенерировано")
        expect(page.locator("[data-public-fill]")).to_have_count(0)
        assert navigations  # Publication caused the browser to reload itself.
        if evidence:
            page.screenshot(path=str(Path(evidence) / f"ready-{width}.png"), full_page=True)
        assert len(submissions) == 1
        assert len(queries.rows("jobs")) == 1
        assert not errors, errors
        browser.close()


def test_lost_post_response_and_status_failures_still_reveal_committed_data(database):
    settings, _, _, queries, _, service, queue = setup(database)
    app = create_app(settings)
    app.state.context.jobs = service
    with serving(app) as origin, sync_playwright() as p:
        settings.site_url = origin + settings.base_path
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page()
        post_count = 0
        status_count = 0

        def lose_post_response(route):
            nonlocal post_count
            post_count += 1
            response = route.fetch()  # The server accepts and commits the job.
            assert response.status == 202
            route.abort("failed")

        def fail_status_temporarily(route):
            nonlocal status_count
            status_count += 1
            if status_count == 1:
                route.fulfill(status=503, body="Temporary outage")
            elif status_count == 2:
                route.fulfill(status=429, body="Slow down")
            elif status_count == 3:
                route.fulfill(status=200, content_type="application/json", body="not-json")
            else:
                route.continue_()

        page.route("**/api/locations/ivanovo/fill", lose_post_response)
        page.route("**/api/locations/ivanovo/fill/status", fail_status_temporarily)
        page.goto(origin + "/guide/ivanovo")
        with page.expect_event("requestfailed", predicate=lambda r: r.method == "POST"):
            page.get_by_role("button", name="Запарсить эти данные").click()
        page.wait_for_function("document.querySelector('form[hidden]') !== null")
        expect(page.locator("[data-fill-waiting]")).to_be_visible()
        expect(page.get_by_role("button", name="Запарсить эти данные")).to_be_hidden()
        assert Worker(queue, service).once()
        expect(page.get_by_role("heading", name="Тестовый ресторан")).to_be_visible(timeout=15000)
        assert post_count == 1
        assert status_count >= 4
        assert len(queries.rows("jobs")) == 1
        browser.close()


def test_failed_job_stops_loading_and_does_not_offer_duplicate_public_generation(database):
    from integration.test_jobs import FakeAI

    from involta_guide.infrastructure.ai.errors import ProviderUnavailable

    class UnavailableAI(FakeAI):
        def generate(self, prompt, job_id):
            raise ProviderUnavailable("OpenAI HTTP 429")

    settings, _, _, queries, _, service, queue = setup(database, UnavailableAI())
    app = create_app(settings)
    app.state.context.jobs = service
    with serving(app) as origin, sync_playwright() as p:
        settings.site_url = origin + settings.base_path
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page(reduced_motion="reduce")
        page.goto(origin + "/guide/ivanovo")
        with page.expect_response("**/api/locations/ivanovo/fill") as submission:
            page.get_by_role("button", name="Запарсить эти данные").click()
        assert submission.value.status == 202
        expect(page.locator("[data-fill-waiting]")).to_be_visible()
        assert (
            page.locator(".public-fill-spinner circle").first.evaluate(
                "el=>getComputedStyle(el).animationName"
            )
            == "none"
        )
        assert Worker(queue, service).once()
        expect(page.get_by_role("status")).to_contain_text("429", timeout=10000)
        expect(page.locator("[data-fill-waiting]")).to_be_hidden()
        expect(page.locator("[data-fill-spoiler]")).to_be_hidden()
        expect(page.get_by_role("button", name="Запарсить эти данные")).to_be_hidden()
        page.reload()
        expect(page.locator("[data-fill-waiting]")).to_be_hidden()
        expect(page.get_by_role("status")).to_contain_text("429")
        assert len(queries.rows("jobs")) == 1
        browser.close()


def test_publication_between_page_content_and_status_reads_reloads_to_fresh_content(database):
    settings, _, _, queries, _, service, queue = setup(database)
    stale_page = queries.public("ivanovo")
    service.enqueue_public_fill(stale_page["location"]["id"])
    assert Worker(queue, service).once()
    app = create_app(settings)
    app.state.context.jobs = service
    public = app.state.context.queries.public
    reads = 0

    def once_stale(slug):
        nonlocal reads
        reads += 1
        return stale_page if reads == 1 else public(slug)

    app.state.context.queries.public = once_stale
    with serving(app) as origin, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page()
        page.goto(origin + "/guide/ivanovo")
        expect(page.get_by_role("heading", name="Тестовый ресторан")).to_be_visible()
        expect(page.locator(".city-hero .lead")).to_have_text("Сгенерировано")
        assert reads >= 2
        browser.close()
