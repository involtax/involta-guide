from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.infrastructure.voice.consumer import Consumer
from involta_guide.infrastructure.voice.read_model import VoiceReadModel
from involta_guide.main import create_app

from .test_web import login
from .voice_fixture import imported


def test_public_latest_ten_are_automatic_primary_text_only(database):
    settings, engine, _, queries, city, ref, event = imported(database)
    for n in range(12):
        Consumer(engine, None).import_event(
            {
                **event,
                "event_id": str(uuid4()),
                "reference": str(uuid4()),
                "sequence": n + 2,
                "transcript": "GigaAM отзыв " + str(n),
                "created_at": f"2026-11-03T01:{n:02d}:00Z",
            }
        )
    read = VoiceReadModel(engine)
    reviews = read.public_texts(city["id"])
    assert len(reviews) == 10
    assert [r["transcript"] for r in reviews] == [
        "GigaAM отзыв " + str(n) for n in reversed(range(2, 12))
    ]
    assert len(read.listing(city["slug"])["rows"]) == 13
    with TestClient(create_app(settings)) as client:
        page = client.get("/guide/" + city["slug"])
        assert page.status_code == 200
        assert "GigaAM отзыв 11" in page.text and "GigaAM отзыв 1</p>" not in page.text
        assert "Parakeet" not in page.text
        path = (
            "/guide/logs/reviews/" + city["slug"] + "/" + str(queries.get("reviews", ref)["number"])
        )
        assert client.get(path).status_code == 401
        login(client)
        assert client.get(path).status_code == 200
        assert client.get("/guide/admin/reviews").text.count('class="review-card"') == 13


def test_city_feed_search_pagination_empty_city_and_removed_actions(database):
    settings, engine, _, queries, city, ref, event = imported(database)
    for n in range(23):
        Consumer(engine, None).import_event(
            {**event, "event_id": str(uuid4()), "reference": str(uuid4()), "sequence": n + 2}
        )
    other = queries.directory(query="Кострома")["rows"][0]
    Consumer(engine, None).import_event(
        {
            **event,
            "event_id": str(uuid4()),
            "reference": str(uuid4()),
            "sequence": 30,
            "metadata": {
                **event["metadata"],
                "location": {
                    **event["metadata"]["location"],
                    "id": other["id"],
                    "slug": other["slug"],
                    "name": other["name"],
                },
            },
            "created_at": "2026-11-04T00:00:00Z",
        }
    )
    read = VoiceReadModel(engine)
    assert read.cities()[0]["slug"] == other["slug"]
    assert read.listing(city["slug"], page=2)["total"] == 24
    assert len(read.listing(city["slug"], page=2)["rows"]) == 4
    assert read.listing(query="no-match")["city"] is None
    with TestClient(create_app(settings)) as client:
        login(client)
        page = client.get("/guide/admin/reviews/" + city["slug"])
        assert page.status_code == 200 and page.text.count('class="review-card"') == 20
        assert client.get("/guide/admin/reviews?q=no-match").status_code == 200
        assert client.get("/guide/admin/reviews?unknown=ignored").status_code == 200
        assert client.get("/guide/admin/reviews/not-found").status_code == 404
        assert (
            client.post(
                "/guide" + read.path(ref) + "/actions", data={"action": "approve"}
            ).status_code
            == 404
        )
    assert len(queries.rows("reviews")) == 25
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM review_decisions")).scalar_one() == 0
