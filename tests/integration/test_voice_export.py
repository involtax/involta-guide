import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.infrastructure.voice.consumer import Consumer
from involta_guide.main import create_app
from involta_voice.contracts import LocationContext
from involta_voice.provider import FakeProvider
from involta_voice.service import VoiceService
from involta_voice.worker import Worker

from .test_jobs import setup
from .test_review_routes import add_review
from .test_voice_guide import client_for
from .test_voice_service import metadata, recording
from .test_voice_service import voice_database as voice_database
from .test_web import login


@pytest.mark.parametrize("mime", ["audio/wav", "audio/webm"])
def test_beta_export_original_ranges_privacy_deletion_and_alpha_gate(
    database, voice_database, streaming_webm, mime
):
    settings, guide, _, queries, city, _, _ = setup(database)
    vs, ve = voice_database
    vs = vs.model_copy(update={"asr_beta_enabled": True})
    settings = settings.model_copy(update={"voice_service_secret": vs.service_secret})
    meta = metadata().model_copy(
        update={
            "location": LocationContext(
                id=city["id"], slug=city["slug"], name=city["name"], content_epoch=1
            )
        }
    )
    source = recording() if mime == "audio/wav" else streaming_webm.read_bytes()
    ref = VoiceService(ve, vs).submit(meta, source, mime)["reference"]
    assert Worker(ve, vs, FakeProvider()).once()
    assert Consumer(guide, client_for(settings, vs)).once() == 2
    review = queries.get("reviews", ref)
    failed = add_review(guide, city["id"])
    deleted = add_review(guide, city["id"])
    with guide.begin() as conn:
        conn.execute(
            text("UPDATE reviews SET status='failed',publication_status=0 WHERE id=:id"),
            {"id": failed["id"]},
        )
        conn.execute(
            text("UPDATE reviews SET status='deleted',publication_status=0 WHERE id=:id"),
            {"id": deleted["id"]},
        )
    app = create_app(settings)
    app.state.voice_client = client_for(settings, vs)
    root = "/guide/logs/reviews/ivanovo/" + str(review["number"])
    with TestClient(app) as client:
        index = client.get("/guide/logs/reviews.json?limit=1")
        assert index.status_code == 200
        first = index.json()
        assert len(first["reviews"]) == 1 and first["next_after"] == review["number"]
        second = client.get(
            "/guide/logs/reviews.json", params={"after": first["next_after"], "limit": 1}
        ).json()
        assert second["reviews"][0]["publication_status"] == 0 and second["next_after"] is None
        assert not any(
            key in first["reviews"][0] for key in ("ip", "user_agent", "visitor", "metadata")
        )
        detail = client.get(root + ".json")
        assert detail.status_code == 200 and detail.headers["cache-control"] == "no-store"
        data = detail.json()
        assert data["voice"]["sha256"] == hashlib.sha256(source).hexdigest()
        assert "metadata" not in data["detail"]["link"] and "ip" not in data["detail"]["review"]
        assert vs.service_secret.get_secret_value() not in detail.text
        assert client.get(root).status_code == 200
        playback = client.get(data["audio_url"], params={"playback": "true"})
        assert playback.status_code == 200
        partial = client.get(data["audio_url"] + "?playback=true", headers={"range": "bytes=0-63"})
        assert partial.status_code == 206 and partial.content == playback.content[:64]
        if mime == "audio/webm":
            import io

            import av

            assert playback.content != source
            with av.open(io.BytesIO(playback.content)) as container:
                assert abs(container.duration / av.time_base - 6.2) < 0.05

        original = client.get(data["audio_url"])
        assert original.content == source and original.headers["accept-ranges"] == "bytes"
        chunk = client.get(data["audio_url"], headers={"range": "bytes=10-39"})
        assert chunk.status_code == 206 and chunk.content == source[10:40]
        assert chunk.headers["content-range"] == f"bytes 10-39/{len(source)}"
        assert (
            client.get(data["audio_url"], headers={"range": "bytes=99999999-"}).status_code == 416
        )
        assert (
            client.get(
                "/guide/logs/reviews/ivanovo/" + str(deleted["number"]) + "/audio"
            ).status_code
            == 404
        )
        app.state.context.settings.public_fill_logs_enabled = False
        for path in [
            "/guide/logs",
            "/guide/logs/reviews.json",
            root,
            root + ".json",
            root + "/audio",
        ]:
            assert client.get(path).status_code == 404, path
        login(client)
        assert client.get(root + "/audio").content == source
        page = client.get("/guide/admin/reviews/ivanovo")
        hrefs = __import__("re").findall(r'href="([^"]+)"', page.text)
        assert "/guide/logs" in hrefs
        assert not any("/logs/reviews/" in href for href in hrefs)
        assert "nodownload noplaybackrate noremoteplayback" in page.text
        assert "/admin/reviews/ivanovo/" + str(review["number"]) + "/audio" not in page.text
        assert (
            client.get(
                "/guide/admin/reviews/ivanovo/" + str(review["number"]) + "/audio"
            ).status_code
            == 404
        )
        with guide.begin() as conn:
            conn.execute(
                text("UPDATE reviews SET status='deleted',publication_status=0 WHERE id=:id"),
                {"id": ref},
            )
        client.cookies.clear()
        app.state.context.settings.public_fill_logs_enabled = True
        for path in [root, root + ".json", root + "/audio"]:
            assert client.get(path).status_code == 404
        with ve.connect() as conn:
            stored = conn.execute(
                text("SELECT audio FROM submissions WHERE reference=:id"), {"id": ref}
            ).scalar_one()
        assert VoiceService(ve, vs).storage.path(stored).read_bytes() == source
