from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.main import create_app

from .test_vip_visitors import vip_rows, watched
from .test_web import login


def header(response):
    assert response.status_code == 200
    assert "private" in response.headers["cache-control"]
    assert "no-store" in response.headers["cache-control"]
    assert "cookie" in response.headers["vary"].lower()
    return response.text.split('<header class="header">', 1)[1].split("</header>", 1)[0]


def test_gold_uses_existing_vip_record_and_never_trusts_a_cookie_flag(database):
    settings, engine = database
    visitor = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO vip_visitors(visitor_id,level) VALUES(:id,1)"), {"id": visitor}
        )
    with TestClient(create_app(settings)) as client:
        for cookie in ("", "guide_visitor=GOLD", f"guide_visitor={uuid4()}; guide_vip=1"):
            page = header(client.get("/guide/", headers={"cookie": cookie}))
            assert "Россия" in page and "brand-gold" not in page
        headers = {"cookie": "guide_visitor=" + visitor}
        for path in ("/guide/", "/guide/ivanovo", "/guide/admin"):
            page = header(client.get(path, headers=headers))
            assert 'class="brand-gold">GOLD' in page and 'class="brand-crown"' in page
            assert "Россия" not in page
        for path, label in (("/guide/about", "О проекте"), ("/guide/for-agents", "MCP")):
            page = header(client.get(path, headers=headers))
            assert label in page and "brand-gold" not in page
        assert vip_rows(engine) == [(visitor, 1)]


def test_gold_appears_after_watched_and_disappears_after_revocation(database):
    settings, engine, _, review, _, service, worker, provider, _ = watched(database)
    visitor = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE reviews SET visitor=:visitor WHERE id=:id"),
            {"visitor": visitor, "id": review},
        )
    headers = {"cookie": "guide_visitor=" + visitor}
    try:
        with TestClient(create_app(settings)) as client:
            assert "brand-gold" not in header(client.get("/guide/", headers=headers))
            assert worker.once()
            assert "brand-gold" in header(client.get("/guide/ivanovo", headers=headers))
            service.delete(review, "test-admin")
            assert "brand-gold" not in header(client.get("/guide/", headers=headers))
            assert not vip_rows(engine)
    finally:
        provider.close()


def test_admin_session_does_not_grant_gold_but_can_coexist_with_it(database):
    settings, engine = database
    with TestClient(create_app(settings)) as client:
        login(client)
        page = header(client.get("/guide/"))
        assert "header-admin-link" in page and "brand-gold" not in page
        visitor = str(uuid4())
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO vip_visitors(visitor_id,level) VALUES(:id,1)"), {"id": visitor}
            )
        headers = {
            "cookie": f"guide_visitor={visitor}; guide_admin={client.cookies.get('guide_admin')}"
        }
        page = header(client.get("/guide/", headers=headers))
        assert "header-admin-link" in page and "brand-gold" in page
        page = header(client.get("/guide/admin", headers=headers))
        assert "admin-logout" in page and "brand-gold" in page
        assert "header-admin-link" not in page
