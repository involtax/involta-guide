import os
import re
import subprocess

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
    assert 'class="admin-tabs"' in result.text
    return token(result.text)


def test_public_admin_htmx_and_csrf(database):
    settings, engine = database
    with TestClient(create_app(settings)) as client:
        redirect = client.get("/guide?q=test", follow_redirects=False)
        assert redirect.status_code == 302 and redirect.headers["location"] == "/guide/?q=test"
        home = client.get("/guide/")
        assert home.status_code == 200 and "Поиск локаций и ресторанов" in home.text
        assert "htmx" not in home.text and "react" not in home.text and "/_next/" not in home.text
        page = client.get("/guide/ivanovo")
        assert page.status_code == 200 and "noindex" in page.text
        assert client.get("/guide/unknown-404").status_code == 404
        assert client.get("/guide/gazprom-laura", follow_redirects=False).status_code == 308
        assert client.get("/guide/admin/reviews/ivanovo/1/audio").status_code == 404
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


def test_production_http_checker_against_real_server(database):
    from .test_voice_upload_transport import serving

    settings, _ = database
    settings = settings.model_copy(update={"release_sha": "a" * 40})
    with serving(create_app(settings)) as origin:
        result = subprocess.run(
            ["bash", "deploy/check-release.sh", origin + "/guide/", settings.release_sha, "local"],
            env={**os.environ, "NO_PROXY": "127.0.0.1,localhost"},
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert result.returncode == 0, result.stdout + result.stderr
