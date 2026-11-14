"""Synthetic regression for a regional map URL; no production data or model calls."""

import json
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient

from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app

from .test_incremental_content import research
from .test_jobs import setup
from .test_public_fill import HEADERS

PAYLOAD = Path(__file__).resolve().parents[1] / "fixtures/regional-yandex-discovery.json"


def test_public_fill_accepts_regional_map_and_continues_to_research(database):
    discovery = json.loads(PAYLOAD.read_text())

    class Replay:
        model = "local-saved-response-no-network"

        def generate(self, prompt, job_id):
            job = queries.get("jobs", job_id)
            operation = job["report"]["operation"]
            if operation == "intro":
                return {"intro": "Тестовое вступление: город на Волге."}
            if operation == "discover":
                return deepcopy(discovery)
            row = queries.get("restaurants", job["report"]["restaurant_id"])
            result = research()
            result["card"].update(
                name=row["name"],
                mapsUrl=row["maps_url"] or f"https://yandex.ru/maps/org/fixture/{row['number']}/",
            )
            return result

    settings, _, uow, queries, city, service, queue = setup(database, Replay())
    with uow() as work:
        work.repo.change(
            "locations",
            city["id"],
            {"name": "Рыбинск", "slug": "rybinsk", "region": "Ярославская область"},
        )
    with TestClient(create_app(settings)) as client:
        client.app.state.context.jobs = service
        assert client.post("/guide/api/locations/rybinsk/fill", headers=HEADERS).status_code == 202
        worker = Worker(queue, service)
        assert worker.once()  # intro
        assert worker.once()  # the synthetic discovery batch
        jobs = queries.rows("jobs")
        search = next(j for j in jobs if j["report"]["operation"] == "discover")
        assert search["status"] == "done", search["error"]
        rows = queries.rows("restaurants")
        assert len(rows) == len(discovery["restaurants"]) == 2
        assert {r["name"] for r in rows} == {r["name"] for r in discovery["restaurants"]}
        regional = next(r for r in rows if r["name"] == "Второй тестовый ресторан")
        assert regional["maps_url"] == "https://yandex.com.ge/maps/org/fixture/2/"
        followups = [j for j in jobs if j["report"]["operation"] == "research"]
        assert len(followups) == 2 and all(j["status"] == "queued" for j in followups)
        assert client.get("/guide/api/locations/rybinsk/fill/status").json()["state"] == "queued"
        assert worker.once()  # local fixture research, not a model or fact-check of the restaurant
        assert client.get("/guide/api/locations/rybinsk/fill/status").json()["state"] == "ready"
        assert len(queries.public("rybinsk")["content"]["restaurants"]) == 1
        page = client.get("/guide/rybinsk")
        assert "AI response validation failed" not in page.text
        assert "Запарсить эти данные" not in page.text
        assert "Тестовое вступление" in page.text
