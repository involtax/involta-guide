from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.infrastructure.voice.read_model import VoiceReadModel
from involta_guide.main import create_app

from .test_jobs import setup
from .test_web import login


def add_review(engine, city):
    key = str(uuid4())
    with engine.begin() as conn:
        return dict(
            conn.execute(
                text("""INSERT INTO reviews(id,location_id,visitor,audio,mime,hash,ip,user_agent)
            VALUES(:id,:city,'owner','kept-original.wav','audio/wav','sha','ip','test') RETURNING *"""),
                {"id": key, "city": city},
            )
            .mappings()
            .one()
        )


def test_review_numbers_survive_sorting_slug_changes_and_concurrent_creation(database):
    settings, engine, _, _, city, _, _ = setup(database)
    with ThreadPoolExecutor(4) as pool:
        reviews = list(pool.map(lambda _: add_review(engine, city["id"]), range(8)))
    numbers = {r["number"] for r in reviews}
    assert len(numbers) == 8 and min(numbers) > 0
    read = VoiceReadModel(engine)
    original = reviews[0]
    path = read.path(original["id"])
    assert path == f"/admin/reviews/ivanovo/{original['number']}"
    assert read.resolve("ivanovo", original["number"])[0] == str(original["id"])
    with TestClient(create_app(settings)) as client:
        assert client.get("/guide" + path).status_code == 401
        login(client)
        page = client.get("/guide" + path)
        assert page.status_code == 200, page.text
        assert page.text.count('class="review-transcripts"') == 8
        assert "<dt>GigaAM<small>onnx-asr</small></dt>" in page.text and "Parakeet" not in page.text
        assert "admin.css" in page.text and "?tab=" not in page.text
        assert client.get("/guide" + path, headers={"HX-Request": "true"}).status_code == 200
        for route in ("/admin/reviews", "/admin/reviews?page=2", path):
            assert client.get("/guide" + route).status_code == 200
        assert read.path(original["id"]) == path
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE locations SET slug='ivanovo-new' WHERE id=:id"), {"id": city["id"]}
            )
        assert client.get("/guide" + path).status_code == 404
        new_path = read.path(original["id"])
        assert new_path == path.replace("ivanovo/", "ivanovo-new/")
        assert client.get("/guide" + new_path).status_code == 200
        assert (
            client.get("/guide/admin/reviews/other-city/" + str(original["number"])).status_code
            == 404
        )
        assert (
            client.get("/guide/admin/reviews/ivanovo-new/" + str(original["id"])).status_code == 404
        )


def test_saved_model_comparison_survives_settings_failure_and_shows_outages(database):
    from pydantic import SecretStr

    from involta_guide.infrastructure.voice.client import VoiceUnavailable

    settings, engine, _, queries, city, _, _ = setup(database)
    review = add_review(engine, city["id"])
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO voice_links(review_id,reference,location_id,number,active_run,metadata) VALUES(:id,:id,:loc,1,1,'{}')"
            ),
            {"id": review["id"], "loc": city["id"]},
        )
    settings = settings.model_copy(
        update={"voice_service_secret": SecretStr("test-voice-secret-" + "x" * 40)}
    )
    calls = []

    class Remote:
        failed = False

        def json(self, method, path, **kwargs):
            calls.append((method, path))
            if path == "/admin/settings" or self.failed:
                raise VoiceUnavailable("Сервис временно недоступен")
            assert path == "/admin/recognitions"
            return {
                str(review["id"]): [
                    {"name": name, "text": phrase, "state": "completed"}
                    for name, phrase in [
                        ("GigaAM", "Первый сохранённый текст"),
                        ("Parakeet", "Второй сохранённый текст"),
                    ]
                ]
            }

    remote = Remote()
    app = create_app(settings)
    app.state.voice_client = remote
    path = "/guide" + VoiceReadModel(engine).path(str(review["id"]))
    with TestClient(app) as client:
        login(client)
        page = client.get(path)
        assert page.status_code == 200
        assert (
            "Первый сохранённый текст" in page.text and "Второй сохранённый текст" not in page.text
        )
        remote.failed = True
        unavailable = client.get(path)
        assert unavailable.status_code == 200
        assert (
            "<dt>GigaAM<small>onnx-asr</small></dt>" in unavailable.text
            and "Parakeet" not in unavailable.text
        )
        assert "Voice временно недоступен" in unavailable.text
    assert all(method == "POST" and path == "/admin/recognitions" for method, path in calls)
    assert queries.rows("jobs") == []
