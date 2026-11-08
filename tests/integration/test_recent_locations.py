from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie

from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.main import create_app
from involta_guide.web.http import RECENT_LOCATIONS_COOKIE

from .test_home_reviews import add_review
from .test_location_search import add_location


def test_public_location_stores_history_without_changing_home_feed_or_enqueuing_jobs(database):
    settings, engine = database
    settings = settings.model_copy(update={"temporary_public_fill_enabled": True})
    first = add_location(engine, "Первый город", "history-first")
    second = add_location(engine, "Второй город", "history-second")
    now = datetime.now(UTC)
    add_review(engine, first, "Старый отзыв просмотренного города", now - timedelta(days=1))
    add_review(engine, second, "Новый отзыв другого города", now)
    with engine.connect() as conn:
        jobs_before = set(conn.execute(text("SELECT id FROM jobs")).scalars())
    with TestClient(create_app(settings)) as client:
        response = client.get("/guide/history-first")
        assert response.status_code == 200
        assert client.cookies.get(RECENT_LOCATIONS_COOKIE) == str(first)
        assert client.cookies.get("guide_visitor")
        assert response.headers["cache-control"] == "private, no-store"
        assert "cookie" in response.headers["vary"].lower()
        response = client.get("/guide/history-second")
        assert client.cookies.get(RECENT_LOCATIONS_COOKIE) == f"{second}.{first}"
        client.get("/guide/history-first")
        assert client.cookies.get(RECENT_LOCATIONS_COOKIE) == f"{first}.{second}"
        home = client.get("/guide/")
        assert home.text.index("Новый отзыв другого города") < home.text.index(
            "Старый отзыв просмотренного города"
        )
        for path, expected_status in [
            ("/guide/", 200),
            ("/guide/history-missing", 404),
            ("/guide/gazprom-laura", 308),
            ("/guide/api/locations/catalogue", 200),
            ("/guide/api/locations/ivanovo/fill/status", 200),
            ("/guide/logs", 200),
            ("/guide/admin", 200),
        ]:
            result = client.get(path, follow_redirects=False)
            assert result.status_code == expected_status, path
            assert all(
                not header.startswith(RECENT_LOCATIONS_COOKIE + "=")
                for header in result.headers.get_list("set-cookie")
            )
        assert client.cookies.get(RECENT_LOCATIONS_COOKIE) == f"{first}.{second}"
        cookie = SimpleCookie()
        for header in response.headers.get_list("set-cookie"):
            cookie.load(header)
        assert cookie[RECENT_LOCATIONS_COOKIE]["max-age"] == str(90 * 86400)
    with engine.connect() as conn:
        assert set(conn.execute(text("SELECT id FROM jobs")).scalars()) == jobs_before
