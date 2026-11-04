from uuid import uuid4

from fastapi.testclient import TestClient

from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.main import create_app

from .test_jobs import setup


def test_public_logs_show_only_deliberate_public_fill_and_redact_secrets(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    service.enqueue_public_fill(location["id"])
    public_job = queries.rows("jobs")[0]
    other = queries.directory(query="Абрау-Дюрсо")["rows"][0]
    with uow() as tx:
        tx.repo.queue(
            {
                "id": str(uuid4()),
                "kind": "generate",
                "location_id": other["id"],
                "prompt": "private legacy prompt",
                "public_log": False,
            }
        )
    admin_job = queries.rows("jobs", {"location_id": other["id"]})[0]
    private_id = str(uuid4())
    with uow() as tx:
        tx.repo.queue(
            {
                "id": private_id,
                "kind": "reset",
                "public_log": False,
                "location_id": location["id"],
                "prompt": "private voice text",
            }
        )
    audit = AIAudit(engine, settings)
    audit.record(
        public_job["id"],
        "response",
        {
            "status": 200,
            "body": '{"result":"Открытый ответ <script>alert(1)</script>"}',
            "headers": {"set-cookie": "secret-cookie"},
        },
    )
    audit.record(
        public_job["id"],
        "job_failed",
        {
            "traceback": "password="
            + settings.session_secret.get_secret_value()
            + " "
            + settings.database_url.get_secret_value(),
            "validation_errors": [{"loc": ["restaurants", 0, "phone"], "msg": "Неверный телефон"}],
        },
    )
    audit.record(admin_job["id"], "response", {"body": "private admin response"})
    audit.record(private_id, "response", {"body": "private voice response"})
    with TestClient(create_app(settings)) as client:
        index = client.get("/guide/logs")
        assert index.status_code == 200
        assert "/logs/ivanovo/1" in index.text and admin_job["id"] not in index.text
        assert private_id not in index.text
        assert "noindex" in index.headers["x-robots-tag"]
        page = client.get("/guide/logs/ivanovo/1")
        assert page.status_code == 200, page.text
        assert "Открытый ответ" in page.text and "Неверный телефон" in page.text
        assert "<script>alert" not in page.text
        exported = client.get("/guide/logs/ivanovo/1" + ".json")
        assert exported.status_code == 200
        assert exported.json()["job"]["id"] == public_job["id"]
        assert "secret-cookie" not in exported.text
        assert settings.session_secret.get_secret_value() not in exported.text
        assert settings.database_url.get_secret_value() not in exported.text
        status = client.get("/guide/logs/ivanovo/1/status")
        assert status.status_code == 200 and status.json()["job"]["stage_label"] == "В очереди"
        assert "prompt" not in status.text and "private" not in status.text
        assert client.get("/guide/logs/ivanovo/2/status").status_code == 404
        assert client.get("/guide/logs/abrau-dyurso/1/status").status_code == 404
        for key in (admin_job["id"], private_id, str(uuid4())):
            assert client.get("/guide/logs/" + key).status_code == 404
            assert client.get("/guide/logs/" + key + ".json").status_code == 404
        assert "Открыть логи наполнения" not in client.get("/guide/ivanovo").text
        assert "Disallow: /guide/logs" in client.get("/guide/robots.txt").text
    disabled = settings.model_copy(update={"public_fill_logs_enabled": False})
    with TestClient(create_app(disabled)) as client:
        assert client.get("/guide/logs").status_code == 404
        assert client.get("/guide/logs/ivanovo/1/status").status_code == 404
        assert client.get("/guide/logs/ivanovo/1" + ".json").status_code == 404
