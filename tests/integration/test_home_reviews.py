from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.infrastructure.voice.read_model import VoiceReadModel
from involta_guide.main import create_app

from .test_location_search import add_location
from .voice_fixture import classified


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
    if status == "transcribed":
        classified(engine, reference)
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
    assert [row["transcript"] for row in rows] == [f"Публичный отзыв {i}" for i in range(11, 1, -1)]
    assert {row["location_name"] for row in rows} == {"Первый город", "Второй город"}
    assert all(
        set(row)
        == {
            "transcript",
            "created_at",
            "number",
            "location_name",
            "location_slug",
            "author_name",
            "restaurant_name",
        }
        for row in rows
    )
    with TestClient(create_app(settings)) as client:
        home = client.get("/guide/")
        assert home.text.count('class="home-review"') == 10
        assert "Расшифровка…" not in home.text
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
        assert "Пульс:" in empty.text and "Пока нет отзывов." in empty.text
        assert 'class="home-review"' not in empty.text
        transcript = '<img src=x onerror="window.__reviewXss=true"> Вкусный суп.'
        add_review(engine, city, transcript, datetime(2026, 11, 6, 23, 30, tzinfo=UTC))
        home = client.get("/guide/")
        assert transcript not in home.text and "&lt;img" in home.text
        assert "7 ноя · 02:30" in home.text and "2026-11-07T02:30:00+03:00" in home.text
        assert "ЕДА. ЛЮДИ. КАЧЕСТВО." in home.text
        assert "Куда отправимся?" not in home.text and "Прислушиваемся к вам." not in home.text
        assert "Пульс:" in client.get("/guide/?q=%20%20").text


def prepared_pulse(database, *, restaurant_name="Листва", author="Иван"):
    """Two confirmed mentions: the first is neutral and outside the public top."""
    from .test_reviews2 import RESULT, prepared

    first, second = str(uuid4()), str(uuid4())
    source = f"Меня зовут {author}. Были в {restaurant_name}. Затем во Втором месте, вкусно."
    answer = {
        **RESULT,
        "author_name": author,
        "edited_text": source.split(". ", 1)[1],
        "mentions": [
            {
                "name": name,
                "restaurant_id": identity,
                "sentiment": sentiment,
                "suggested": False,
                "evidence": name,
                "source_name": name,
            }
            for name, identity, sentiment in [
                (restaurant_name, first, None),
                ("Втором месте", second, 1),
            ]
        ],
    }
    context = prepared(database, answer)
    _, engine, city, review, *_ = context
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE reviews SET transcript=:source WHERE id=:id"),
            {"source": source, "id": review},
        )
        for identity, name in [(first, restaurant_name), (second, "Втором месте")]:
            conn.execute(
                text(
                    "INSERT INTO restaurants(id,location_id,name,active) VALUES(:id,:loc,:name,0)"
                ),
                {"id": identity, "loc": city["id"], "name": name},
            )
    return context


def test_home_header_shows_known_restaurant_before_approval_but_votes_wait_for_ok(database):
    from .test_reviews2 import state

    settings, engine, city, review, _, service, worker, provider, calls = prepared_pulse(database)
    read = VoiceReadModel(engine)
    try:
        assert worker.once()
        analysis = state(engine, review)
        assert analysis["result"] and not analysis["approved_at"]
        assert read.latest_public_texts()[0]["restaurant_name"] == "Листва"
        assert read.latest_public_texts()[0]["author_name"] is None
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM review_restaurant_votes")).scalar_one() == 0
            )
            assert (
                conn.execute(
                    text("SELECT applied FROM jobs WHERE id=:id"), {"id": analysis["job_id"]}
                ).scalar_one()
                is False
            )
        with TestClient(create_app(settings)) as client:
            before = client.get("/guide/").text
            assert 'class="home-review-restaurant" title="Листва">Листва</span>' in before
            assert 'class="home-review-author" title="Иван">Иван</span>' not in before
        service.approve(review, str(analysis["job_id"]), "admin")
        entry = read.latest_public_texts()[0]
        assert entry["restaurant_name"] == "Листва" and entry["author_name"] == "Иван"
        assert entry["transcript"] == "Были в Листва. Затем во Втором месте, вкусно."
        with engine.connect() as conn:
            # Only the second mention has a vote; it must not replace the first.
            assert conn.execute(text("SELECT count(*) FROM review_restaurant_votes")).scalar() == 1
        with TestClient(create_app(settings)) as client:
            home = client.get("/guide/").text
            assert 'class="home-review-restaurant" title="Листва">Листва</span>' in home
            assert 'class="home-review-author" title="Иван">Иван</span>' in home
            assert f'href="/guide/{city["slug"]}"' in home
            assert 'class="home-review-restaurant" title="Втором месте"' not in home
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE reviews SET transcript='Новая расшифровка' WHERE id=:id"),
                {"id": review},
            )
        assert not read.latest_public_texts()
        assert len(calls) == 1  # All analysis calls are mocked; reading schedules nothing.
    finally:
        provider.client.close()


def test_unknown_suggestion_waits_for_ok_and_failed_analysis_never_supplies_header(database):
    from .test_reviews2 import prepared, state

    settings, engine, city, review, _, service, worker, provider, _ = prepared(database)
    read = VoiceReadModel(engine)
    try:
        assert worker.once()
        analysis = state(engine, review)
        assert read.latest_public_texts()[0]["restaurant_name"] is None
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM restaurants WHERE name='Листва'")
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(text("SELECT count(*) FROM review_restaurant_votes")).scalar_one() == 0
            )
        service.approve(review, str(analysis["job_id"]), "admin")
        assert read.latest_public_texts()[0]["restaurant_name"] == "Листва"
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE jobs SET status='failed' WHERE id=:id"), {"id": analysis["job_id"]}
            )
        assert not read.latest_public_texts()
    finally:
        provider.close()
