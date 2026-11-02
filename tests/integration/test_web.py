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
        fragment = client.get("/guide/admin?tab=locations", headers={"HX-Request": "true"})
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
        assert client.get("/guide/admin/card").status_code == 200
        for tab in ("fill", "reviews", "restaurants", "ratings", "contacts", "sessions"):
            response = client.get("/guide/admin?tab=" + tab)
            assert response.status_code == 200, response.text[:300]
        assert client.get("/guide/health/ready").json()["status"] == "ready"


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
        form = {"locationId": location["id"], "consent": "yes", "duration": "9999"}
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
