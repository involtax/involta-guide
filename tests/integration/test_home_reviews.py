from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.infrastructure.voice.read_model import VoiceReadModel
from involta_guide.main import create_app

from .test_location_search import add_location


def add_review(engine, location, transcript, created_at, *, status="transcribed", publication=1):
    reference = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO reviews(id,location_id,visitor,audio,mime,hash,ip,user_agent,
                transcript,status,publication_status,created_at,content_epoch)
                VALUES(:id,:location,'private-visitor','private-audio.wav','audio/wav','hash',
                'private-ip','private-agent',:transcript,:status,:publication,:created,1)"""),
            {
                "id": reference,
                "location": location,
                "transcript": transcript,
                "status": status,
                "publication": publication,
                "created": created_at,
            },
        )
    return reference


def test_global_feed_has_only_ten_newest_public_reviews_and_public_active_locations(database):
    settings, engine = database
    cities = [
        add_location(engine, name, slug)
        for name, slug in [("Первый город", "feed-first"), ("Второй город", "feed-second")]
    ]
    base = datetime(2026, 11, 6, 10, tzinfo=UTC)
    for i in range(12):
        add_review(engine, cities[i % 2], f"Публичный отзыв {i}", base + timedelta(minutes=i // 2))
    for status, publication in [
        ("failed", 0),
        ("deleted", 1),
        ("transcribed", 0),
        ("transcribed", 2),
    ]:
        add_review(
            engine,
            cities[0],
            "Недоступный отзыв",
            base + timedelta(hours=1),
            status=status,
            publication=publication,
        )
    for slug, options in [("inactive", {"active": 0}), ("hidden", {"listed": False})]:
        city = add_location(engine, "Непубличный город", "feed-" + slug, **options)
        add_review(engine, city, "Непубличная локация", base + timedelta(hours=1))
    pending = add_review(engine, cities[0], None, base + timedelta(hours=2), status="received")
    rows = VoiceReadModel(engine).latest_public_texts()
    assert len(rows) == 10
    assert rows[0]["transcript"] is None
    assert [row["transcript"] for row in rows[1:]] == [
        f"Публичный отзыв {i}" for i in range(11, 2, -1)
    ]
    assert {row["location_name"] for row in rows} == {"Первый город", "Второй город"}
    assert all(
        set(row) == {"transcript", "created_at", "number", "location_name", "location_slug"}
        for row in rows
    )
    with TestClient(create_app(settings)) as client:
        home = client.get("/guide/")
        assert home.text.count('class="home-review"') == 10
        assert "Расшифровка…" in home.text
        assert "Недоступный отзыв" not in home.text and "Непубличная локация" not in home.text
        assert "private-audio" not in home.text and "private-visitor" not in home.text
        assert 'class="city-grid"' not in home.text and 'class="pagination"' not in home.text
        assert 'href="/guide/feed-first"' in home.text and 'href="/guide/feed-second"' in home.text
        assert home.headers["cache-control"] == "private, no-store"
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE reviews SET status='deleted',publication_status=0 WHERE id=:id"),
                {"id": pending},
            )
        assert "Расшифровка…" not in client.get("/guide/").text
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM reviews WHERE id=:id"), {"id": pending}
                ).scalar_one()
                == 1
            )


def test_home_empty_feed_and_escaped_transcript_with_local_time(database):
    settings, engine = database
    city = add_location(engine, "Тестовый город", "feed-safe")
    with TestClient(create_app(settings)) as client:
        empty = client.get("/guide/")
        assert "Жизнь кипит" in empty.text and "Пока нет отзывов." in empty.text
        assert 'class="home-review"' not in empty.text
        transcript = '<img src=x onerror="window.__reviewXss=true"> Вкусный суп.'
        add_review(engine, city, transcript, datetime(2026, 11, 6, 23, 30, tzinfo=UTC))
        home = client.get("/guide/")
        assert transcript not in home.text and "&lt;img" in home.text
        assert "7 ноя · 02:30" in home.text and "2026-11-07T02:30:00+03:00" in home.text
        assert "Еда. Люди. Качество." in home.text
        assert "Куда отправимся?" not in home.text and "Прислушиваемся к вам." not in home.text
        assert "Жизнь кипит" in client.get("/guide/?q=%20%20").text
