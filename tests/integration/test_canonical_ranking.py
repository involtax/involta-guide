from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import inspect

from involta_guide.application.services.editorial import save
from involta_guide.domain.rules import FACTORS
from involta_guide.main import create_app

from .test_editorial import CARD, services
from .test_web import login


def test_public_admin_detail_and_pagination_share_live_order(database):
    settings, engine = database
    editor, queries, factory = services(database)
    city = queries.directory(query="Иваново")["rows"][0]
    cards = [{**CARD, "name": f"Ресторан {i:02d}"} for i in range(23)]
    report = [
        {"name": r["name"], "scores": dict.fromkeys(FACTORS, 8), "totalScore": 10, "evidence": []}
        for r in cards[1:]
    ]
    editor.save(city["id"], {"intro": "Тест", "restaurants": cards, "rankingReport": report}, None)
    rows = sorted(
        queries.rows("restaurants", {"location_id": city["id"]}), key=lambda r: r["number"]
    )
    with factory() as work:
        # Neither a star, renaming, stale position nor historical generation report is live ranking authority.
        work.repo.change("restaurants", rows[2]["id"], {"alex_star": True, "name": "А"})
        work.repo.change("restaurants", rows[-1]["id"], {"active": 0})
        work.repo.add(
            "jobs",
            {
                "id": str(uuid4()),
                "kind": "generate",
                "location_id": city["id"],
                "applied": True,
                "report": {"rankingReport": [{"name": "Obsolete", "totalScore": 10}]},
            },
        )
    expected = [r["id"] for r in rows[1:-1]] + [rows[0]["id"]]
    public = queries.public(city["slug"])["content"]
    admin = queries.restaurant_browser(location_id=city["id"])["restaurant_cards"]
    assert [r["id"] for r in public["restaurants"]] == expected
    assert [r["id"] for r in admin if r["active"]] == expected
    assert admin[-1]["display_rank"] is None
    assert admin[-2]["score"] is None
    assert public["rankingReport"][0]["totalScore"] == 8
    assert public["rankingReport"][1]["name"] == "А"
    assert queries.location(city["id"])["content"] == public
    assert queries.restaurant(rows[0]["number"])["restaurant_cards"][0]["display_rank"] == 22
    assert "position" not in {c["name"] for c in inspect(engine).get_columns("restaurants")}
    revision = queries.get("locations", city["id"])["revision"]
    # Incoming list order cannot create another ranking or a fake change/revision.
    assert (
        editor.save(
            city["id"], {**public, "restaurants": list(reversed(public["restaurants"]))}, revision
        )
        == revision
    )
    assert queries.public(city["slug"])["content"] == public
    with TestClient(create_app(settings)) as client:
        login(client)
        page = client.get(f"/guide/admin/restaurants/{city['slug']}?page=2")
        assert page.status_code == 200
        assert 'class="restaurant-position">21</span>' in page.text
        assert 'class="restaurant-position">22</span>' in page.text
        detail = client.get(f"/guide/admin/restaurants/{rows[0]['number']}")
        assert 'class="restaurant-position">22</span>' in detail.text


def test_generation_stores_criteria_but_total_only_in_source_log(database):
    _, queries, factory = services(database)
    city = queries.directory(query="Иваново")["rows"][0]
    report = [
        {"name": CARD["name"], "scores": dict.fromkeys(FACTORS, 8), "totalScore": 8, "evidence": []}
    ]
    with factory() as work:
        job = work.repo.add(
            "jobs", {"id": str(uuid4()), "location_id": city["id"], "kind": "generate"}
        )
        save(
            work.repo,
            city["id"],
            {"intro": "Тест", "restaurants": [CARD], "rankingReport": report},
            None,
            job_id=job["id"],
        )
    row = queries.rows("restaurants", {"location_id": city["id"]})[0]
    assert "totalScore" not in row["ranking"]
    assert row["ranking"]["scores"] == dict.fromkeys(FACTORS, 8)
    assert queries.get("jobs", job["id"])["report"]["rankingReport"][0]["totalScore"] == 8
    assert queries.public(city["slug"])["content"]["rankingReport"][0]["totalScore"] == 8
