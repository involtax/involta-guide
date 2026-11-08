import os
from pathlib import Path

from fastapi.testclient import TestClient
from integration.test_generation_token_usage import token_log
from playwright.sync_api import expect, sync_playwright

from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app


def test_token_summary_on_mobile_then_desktop(database):
    context, calls = token_log(database)
    settings, engine, _, _, service, queue, provider, job = context
    shots = Path("var/screenshots/generation-tokens")
    shots.mkdir(parents=True, exist_ok=True)
    try:
        assert Worker(queue, service).once()
        detail = AIAudit(engine, settings).detail(job["id"])
        with TestClient(create_app(settings)) as client, sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
            )
            page = browser.new_page(has_touch=True)

            def respond(route):
                response = client.get(route.request.url)
                route.fulfill(
                    status=response.status_code,
                    body=response.content,
                    headers=dict(response.headers),
                )

            page.route("**/*", respond)
            for width in (320, 390, 1440):
                page.set_viewport_size({"width": width, "height": 1000})
                page.goto(
                    "http://testserver/guide" + detail["job"]["log_path"], wait_until="networkidle"
                )
                for key, value in [
                    ("total_tokens", "19000"),
                    ("cached_input_tokens", "10000"),
                    ("reasoning_tokens", "4000"),
                ]:
                    expect(page.locator(f'[data-token-count="{key}"]')).to_have_text(value)
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                page.get_by_role(
                    "heading", name="Расход токенов", exact=True
                ).scroll_into_view_if_needed()
                page.screenshot(path=str(shots / f"summary-{width}.png"))
            assert calls == ["POST"]
            browser.close()
    finally:
        provider.close()
