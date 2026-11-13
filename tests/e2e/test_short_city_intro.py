"""Manual AI samples in the real public template; no model or production writes."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from integration.test_jobs import setup
from playwright.sync_api import sync_playwright

from involta_guide.infrastructure.location_catalog import load_catalog
from involta_guide.main import create_app

ROOT = Path(__file__).resolve().parents[2]


def test_manual_intro_samples_fit_two_lines_in_real_mobile_template(database):
    settings, _, uow, queries, *_ = setup(database)
    samples = json.loads((ROOT / "docs/design/short-city-introductions/samples.json").read_text())
    catalogue = {row["slug"]: row for row in load_catalog(ROOT)["locations"]}
    with uow() as work:
        for sample in samples:
            found = work.repo.by_slug(sample["slug"])
            if found:
                location = found[0]
            else:
                location = work.repo.add(
                    "locations",
                    {k: v for k, v in catalogue[sample["slug"]].items() if k != "parent_slug"},
                )
            work.repo.change(
                "locations",
                location["id"],
                {"intro": sample["intro"], "initialized_at": datetime.now(UTC)},
            )
    shots = ROOT / "var/screenshots/short-city-introductions"
    shots.mkdir(parents=True, exist_ok=True)
    measurements = []
    with TestClient(create_app(settings)) as client, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )

        def respond(route):
            response = client.get(route.request.url)
            route.fulfill(
                status=response.status_code, body=response.content, headers=dict(response.headers)
            )

        for width in (320, 360, 390, 430, 1440):
            page = browser.new_page(
                viewport={"width": width, "height": 920},
                is_mobile=width < 500,
                has_touch=width < 500,
            )
            page.route("**/*", respond)
            for sample in samples:
                page.goto("http://testserver/guide/" + sample["slug"])
                lead = page.locator(".city-hero .lead")
                assert lead.inner_text() == sample["intro"]
                count = len(sample["intro"])
                assert count <= 55
                lines = lead.evaluate(
                    "e=>Math.round(e.getBoundingClientRect().height/parseFloat(getComputedStyle(e).lineHeight))"
                )
                assert lines <= 2, (sample, width, lines)
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                measurements.append(
                    {"viewport": width, "slug": sample["slug"], "characters": count, "lines": lines}
                )
                if sample["slug"] == "medvezhegorsk":
                    page.locator(".city-hero").screenshot(
                        path=str(shots / f"actual-medvezhegorsk-{width}.png")
                    )
            page.close()
        browser.close()
    assert queries.rows("jobs") == []
    (shots / "measurements.json").write_text(
        json.dumps(measurements, ensure_ascii=False, indent=2) + "\n"
    )
