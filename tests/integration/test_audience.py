from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from involta_guide.infrastructure.voice.consumer import Consumer
from involta_guide.main import create_app
from involta_stats import add_hit, add_review, calendar_day, daily_stats

from .test_jobs import setup
from .test_voice_guide import client_for
from .test_voice_service import recording
from .test_voice_service import voice_database as voice_database
from .test_web import login
from .voice_fixture import imported


def test_concurrent_hits_distinct_visitors_rollover_and_retention(database):
    _, engine = database
    now = datetime(2026, 11, 4, 12, tzinfo=UTC)
    visitor = uuid4()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: add_hit(engine, visitor, now=now), range(32)))
    add_hit(engine, uuid4(), now=now)
    yesterday = now - timedelta(days=1)
    add_hit(engine, visitor, now=yesterday)
    with engine.begin() as conn:
        add_review(conn, uuid4(), now, now=now)  # A review-only identity is not a page unique.
        add_review(conn, visitor, yesterday, now=now)
        add_review(conn, visitor, now - timedelta(days=30), now=now)
        add_review(conn, visitor, now + timedelta(days=1), now=now)
        conn.execute(
            text("INSERT INTO audience_daily VALUES(:day,:visitor,99,99)"),
            {"day": calendar_day(now) - timedelta(days=30), "visitor": uuid4()},
        )
    add_hit(engine, visitor, now=now)
    days = daily_stats(engine, now=now)
    assert len(days) == 30
    assert days[0].day == calendar_day(now)
    assert days[-1].day == calendar_day(now) - timedelta(days=29)
    assert (days[0].hits, days[0].uniques, days[0].reviews) == (34, 2, 1)
    assert days[0].hits_per_unique == 17
    assert (days[1].hits, days[1].uniques, days[1].reviews) == (1, 1, 1)
    assert all(day.hits == day.uniques == day.reviews == 0 for day in days[2:])
    with engine.connect() as conn:
        assert conn.execute(text("SELECT min(day) FROM audience_daily")).scalar_one() == days[1].day


def test_public_hits_footer_cookie_and_real_admin_exclusion(database, monkeypatch):
    settings, engine = database
    with TestClient(create_app(settings)) as client:
        assert client.get("/guide/admin/stats").status_code == 401
        for method, url, status in [
            ("GET", "/guide", 302),
            ("HEAD", "/guide/", 405),
            ("GET", "/guide/missing-city", 404),
            ("GET", "/guide/gazprom-laura", 308),
            ("GET", "/guide/assets/style.css", 200),
            ("GET", "/guide/voice/api/v1/reviews?location=ivanovo", 200),
            ("GET", "/guide/logs", 200),
            ("GET", "/guide/health/ready", 200),
            ("GET", "/guide/sitemap.xml", 200),
        ]:
            assert client.request(method, url, follow_redirects=False).status_code == status
        assert sum(day.hits for day in daily_stats(engine)) == 0
        home = client.get("/guide/")
        assert home.status_code == 200
        footer = home.text.split("<footer>")[1].split("</footer>")[0]
        assert "Я тут в первый раз" in footer and "/admin" not in footer
        visitor = UUID(client.cookies.get("guide_visitor"))
        assert "private" in home.headers["cache-control"]
        assert "cookie" in home.headers["vary"].lower()
        assert (
            "HttpOnly" in home.headers["set-cookie"] and "Path=/guide" in home.headers["set-cookie"]
        )
        assert "Max-Age=31536000" in home.headers["set-cookie"]
        about = client.get("/guide/about")
        assert about.status_code == 200 and "<h1>О проекте</h1>" in about.text
        assert client.cookies.get("guide_about_seen") == "1"
        assert "Я тут в первый раз" not in about.text
        assert "О проекте" in client.get("/guide/ivanovo").text.split("<footer>")[1]
        assert UUID(client.cookies.get("guide_visitor")) == visitor
        client.cookies.set("guide_admin", "invalid")
        assert client.get("/guide/").status_code == 200
        assert daily_stats(engine)[0].hits == 4
        assert daily_stats(engine)[0].uniques == 1
        client.cookies.delete("guide_admin")
        login(client)
        assert client.get("/guide/").status_code == 200
        assert client.get("/guide/ivanovo").status_code == 200
        assert client.get("/guide/about").status_code == 200
        stats = client.get("/guide/admin/stats?unknown=ignored")
        assert stats.status_code == 200
        assert stats.text.count("<time datetime=") == 30
        assert stats.text.count('<th scope="row">') == 4
        assert "Хитов на уника" in stats.text and "4,0" in stats.text
        assert "pagination" not in stats.text
        assert daily_stats(engine)[0].hits == 4
    with TestClient(create_app(settings)) as second:
        second.get("/guide/")
        assert daily_stats(engine)[0].uniques == 2

        # An analytics outage must not break a successful public response.
        def unavailable(*args, **kwargs):
            raise OperationalError("offline", {}, None)

        monkeypatch.setattr("involta_guide.web.middleware.audience.add_hit", unavailable)
        assert second.get("/guide/").status_code == 200


@pytest.mark.parametrize("first_kind", ["recording_received", "gigaam_ready"])
def test_reviews_count_once_on_creation_and_roll_back_with_the_review(
    database, first_kind, monkeypatch
):
    _, engine, _, _, _, _, original = imported(database)
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    event = {
        **original,
        "event_id": str(uuid4()),
        "reference": str(uuid4()),
        "sequence": 2,
        "kind": first_kind,
        "created_at": yesterday.isoformat(),
        "metadata": {
            **original["metadata"],
            "visitor": str(uuid4()),
            "statistics_excluded": False,
        },
    }
    consumer = Consumer(engine, None)
    assert consumer.import_event(event) == 1
    assert consumer.import_event(event) == 0
    for sequence, kind in enumerate(("recording_received", "gigaam_ready", "gigaam_failed"), 3):
        consumer.import_event(
            {**event, "event_id": str(uuid4()), "sequence": sequence, "kind": kind}
        )
    assert sum(day.reviews for day in daily_stats(engine)) == 1
    assert daily_stats(engine)[1].reviews == 1  # Acceptance day, not delivery or ASR day.
    assert sum(day.uniques for day in daily_stats(engine)) == 0
    # A later failure of the owning transaction must undo the counter as well.
    from involta_guide.infrastructure.voice import consumer as module

    real_execute = module.execute

    def fail_after_counter(conn, sql, **params):
        if "UPDATE voice_inbox SET state='imported'" in sql:
            raise RuntimeError("rollback after counter")
        return real_execute(conn, sql, **params)

    broken = {**event, "event_id": str(uuid4()), "reference": str(uuid4()), "sequence": 6}
    with monkeypatch.context() as patch:
        patch.setattr(module, "execute", fail_after_counter)
        with pytest.raises(RuntimeError, match="rollback after counter"):
            consumer.import_event(broken)
    assert sum(day.reviews for day in daily_stats(engine)) == 1
    assert consumer.import_event(broken) == 1
    assert sum(day.reviews for day in daily_stats(engine)) == 2


def test_admin_flag_is_frozen_on_acceptance_and_cannot_be_supplied_by_browser(
    database, voice_database
):
    settings, engine, _, _, city, _, _ = setup(database)
    voice_settings, voice_engine = voice_database
    settings = settings.model_copy(
        update={"voice_service_secret": voice_settings.service_secret, "voice_enabled": True}
    )
    app = create_app(settings)
    app.state.voice_client = client_for(settings, voice_settings)
    with TestClient(app) as client:
        client.get("/guide/")
        visitor = client.cookies.get("guide_visitor")
        headers = {"origin": "http://testserver"}

        def upload(forged):
            return client.post(
                "/guide/voice/api/v1/submissions?location=" + city["slug"] + "&content_epoch=1",
                headers=headers,
                data={"idempotency_key": str(uuid4()), "statistics_excluded": forged},
                files={"audio": ("x.wav", recording(), "audio/wav")},
            )

        public = upload("true")
        assert public.status_code == 202, public.text
        login(client)
        admin = upload("false")
        assert admin.status_code == 202, admin.text
        client.cookies.clear()  # Session can change before delivery; the decision is already frozen.
    with voice_engine.connect() as conn:
        metadata = list(
            conn.execute(text("SELECT metadata FROM submissions ORDER BY created_at")).scalars()
        )
        assert [item["statistics_excluded"] for item in metadata] == [False, True]
        assert all(item["visitor"] == visitor for item in metadata)
    consumer = Consumer(engine, app.state.voice_client)
    assert consumer.once() == 2
    assert consumer.once() == 0
    assert daily_stats(engine)[0].reviews == 1
