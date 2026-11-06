import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.infrastructure.security import digest
from involta_guide.main import create_app


@pytest.mark.parametrize("session", ["anonymous", "invalid", "expired", "revoked", "valid"])
def test_public_header_uses_real_admin_session_and_leaves_admin_logout(database, session):
    settings, engine = database
    app = create_app(settings)
    token = None
    if session == "invalid":
        token = "not-a-real-session"
    elif session != "anonymous":
        token = app.state.context.security.login("test-password-123456", "header-test")
        if session == "revoked":
            app.state.context.security.revoke(token)
        elif session == "expired":
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE admin_sessions SET expires_at=now()-interval '1 second' WHERE token_hash=:hash"
                    ),
                    {"hash": digest(token)},
                )
    with TestClient(app) as client:
        if token:
            client.cookies.set("guide_admin", token)
        for path in (
            "/guide/",
            "/guide/ivanovo",
            "/guide/about",
            "/guide/logs",
            "/guide/unknown-location",
        ):
            response = client.get(path)
            header = response.text.split('<header class="header">')[1].split("</header>")[0]
            assert ('class="header-admin-link"' in header) == (session == "valid"), path
            if session == "valid":
                assert 'href="/guide/admin"' in header and "Админка" in header
            assert response.headers["cache-control"] == "private, no-store"
            assert "cookie" in response.headers["vary"].lower()
        if session == "valid":
            admin = client.get("/guide/admin")
            assert "header-admin-link" not in admin.text
            assert "admin-logout" in admin.text and "Выйти" in admin.text
