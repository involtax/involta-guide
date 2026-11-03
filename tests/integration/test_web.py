import io
import re
import wave

from fastapi.testclient import TestClient

from involta_guide.main import create_app


def token(html):
    match = re.search(r'name="_csrf" value="([^"]+)"', html)
    assert match, html[:200]
    return match.group(1)


def login(client):
    page = client.get("/guide/admin")
    result = client.post(
        "/guide/admin/login",
        data={"password": "test-password-123456", "_csrf": token(page.text)},
        headers={"origin": "http://testserver"},
    )
    assert result.status_code == 200, result.text
    assert "Рабочий стол." in result.text
    return token(result.text)


def test_public_admin_htmx_and_csrf(database):
    settings, engine = database
    with TestClient(create_app(settings)) as client:
        redirect = client.get("/guide?q=test", follow_redirects=False)
        assert redirect.status_code == 302 and redirect.headers["location"] == "/guide/?q=test"
        home = client.get("/guide/")
        assert home.status_code == 200 and "Куда отправимся?" in home.text
        assert "htmx" not in home.text and "react" not in home.text and "/_next/" not in home.text
        page = client.get("/guide/ivanovo")
        assert page.status_code == 200 and "noindex" in page.text
        assert client.get("/guide/unknown-404").status_code == 404
        assert client.get("/guide/gazprom-laura", follow_redirects=False).status_code == 308
        assert client.get("/guide/api/admin/audio/missing").status_code == 401
        csrf = login(client)
        fragment = client.get("/guide/admin/locations", headers={"HX-Request": "true"})
        assert (
            fragment.status_code == 200
            and "<!doctype" not in fragment.text
            and 'id="admin-main"' in fragment.text
        )
        assert (
            client.post(
                "/guide/admin/actions/cancel", data={}, headers={"origin": "http://testserver"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/guide/admin/actions/cancel",
                data={"_csrf": csrf},
                headers={"origin": "https://evil.test"},
            ).status_code
            == 403
        )
        assert client.get("/guide/admin/card").status_code == 404
        for tab in ("reviews", "ratings", "contacts"):
            response = client.get("/guide/admin?tab=" + tab)
            assert response.status_code == 200, response.text[:300]
        assert client.get("/guide/health/ready").json()["status"] == "ready"


def test_admin_cookie_and_database_expiry_renew_together_and_logout_stays_final(database):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    from involta_guide.infrastructure.security import ADMIN_SESSION_MAX_AGE, digest

    settings, engine = database
    with TestClient(create_app(settings)) as client:
        csrf = login(client)
        session_token = client.cookies.get("guide_admin")
        cookie = next(c for c in client.cookies.jar if c.name == "guide_admin")
        assert cookie._rest["SameSite"] == "lax"
        assert cookie.expires > (datetime.now(UTC) + timedelta(days=364)).timestamp()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE admin_sessions SET expires_at=now()+interval '1 hour' "
                    "WHERE token_hash=:hash"
                ),
                {"hash": digest(session_token)},
            )
        response = client.get("/guide/health/live")
        renewed_cookie = response.headers["set-cookie"]
        assert f"Max-Age={ADMIN_SESSION_MAX_AGE}" in renewed_cookie
        assert "SameSite=lax" in renewed_cookie
        with engine.connect() as conn:
            expiry = conn.execute(
                text("SELECT expires_at FROM admin_sessions WHERE token_hash=:hash"),
                {"hash": digest(session_token)},
            ).scalar_one()
        assert expiry > datetime.now(UTC) + timedelta(days=364)
        assert "set-cookie" not in client.get("/guide/health/live").headers
        response = client.post(
            "/guide/admin/logout",
            data={"_csrf": csrf},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert client.cookies.get("guide_admin") is None
        client.cookies.set("guide_admin", session_token)
        assert "set-cookie" not in client.get("/guide/health/live").headers


def test_upload_duration_signature_and_history(database):
    settings, engine = database
    app = create_app(settings)
    with TestClient(app) as client:
        location = app.state.context.queries.directory(query="Иваново")["rows"][0]
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\0\0" * 16000)
        form = {"locationId": location["id"], "duration": "9999"}
        response = client.post(
            "/guide/api/reviews",
            data=form,
            files={"audio": ("voice.wav", buffer.getvalue(), "audio/wav")},
            headers={"origin": "http://testserver"},
        )
        assert response.status_code == 201, response.text
        history = client.get("/guide/api/my-reviews?location=" + location["id"]).json()
        assert len(history) == 1
        review = app.state.context.queries.get("reviews", response.json()["id"])
        assert 1.9 < review["duration_seconds"] < 2.1
        bad = client.post(
            "/guide/api/reviews",
            data=form,
            files={"audio": ("fake.wav", b"RIFF" + b"x" * 300, "audio/wav")},
            headers={"origin": "http://testserver"},
        )
        assert bad.status_code == 400
        assert (
            client.post(
                "/guide/api/reviews", data=form, headers={"origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert client.get("/guide/api/admin/audio/" + review["id"]).status_code == 401
        csrf = login(client)
        assert client.get("/guide/api/admin/audio/" + review["id"]).content == buffer.getvalue()
        client.post(
            "/guide/admin/actions/moderate",
            data={"_csrf": csrf, "review_id": review["id"], "decision": "fraud", "reason": "test"},
            headers={"origin": "http://testserver"},
        )
        assert (
            client.get("/guide/api/my-reviews?location=" + location["id"]).json()[0]["fraud"] == 1
        )


def test_alex_accepts_short_unicode_and_special_character_secrets(database):
    import base64
    from urllib.parse import quote

    from pydantic import SecretStr

    from involta_guide.config import Settings

    settings, _ = database
    for secret in ("x", "-", "page", "секрет 🔑", "a&b= c+?#/'\"\\$", "line one\nline two"):
        values = settings.model_dump()
        values.update(
            alex_secret=SecretStr(""),
            alex_secret_base64=SecretStr(base64.b64encode(secret.encode("utf-8")).decode("ascii")),
        )
        configured = Settings(**values)
        with TestClient(create_app(configured)) as client:
            wrong = client.get(
                "/guide/?" + quote(secret + "-wrong", safe=""), follow_redirects=False
            )
            assert wrong.status_code == 200
            assert "guide_alex" not in client.cookies
            response = client.get("/guide/?" + quote(secret, safe=""), follow_redirects=False)
            assert response.status_code == 302
            assert response.headers["location"] == "/guide/"
            token = client.cookies.get("guide_admin")
            assert token
            assert client.app.state.context.security.principal(token) is not None
    empty = settings.model_copy(
        update={"alex_secret": SecretStr(""), "alex_secret_base64": SecretStr("")}
    )
    with TestClient(create_app(empty)) as client:
        assert client.app.state.context.security.alex_login("") is None
        assert client.get("/guide/?anything", follow_redirects=False).status_code == 200
        assert "guide_alex" not in client.cookies
        assert client.get("/guide/?page=not-a-number").status_code == 422
