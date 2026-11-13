from fastapi.testclient import TestClient

from involta_guide.catalogue.service import CityContent
from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app

from .test_incremental_content import ContentAI
from .test_jobs import setup
from .test_web import login


def filled(database):
    settings, engine, uow, queries, city, jobs, queue = setup(database, ContentAI())
    jobs.enqueue_public_fill(city["id"])
    worker = Worker(queue, jobs)
    assert worker.once() and worker.once() and worker.once()
    return settings, engine, uow, queries, city, jobs, queue


def test_initial_fill_is_one_group_and_old_request_urls_remain_valid(database):
    settings, engine, _, queries, city, jobs, queue = filled(database)
    rows = sorted(queries.rows("jobs"), key=lambda r: r["created_at"])
    root = rows[0]["id"]
    assert len(rows) == 3
    assert {r["report"]["log_group"] for r in rows} == {root}
    store = AIAudit(engine, settings)
    listing, group = store.groups(1, job_id=rows[-1]["id"], public_only=True)
    assert listing["total"] == 1 and group["request_count"] == 3
    assert group["stage_label"] == "Готово"
    with TestClient(create_app(settings)) as client:
        for number, row in enumerate(rows, 1):
            path = f"/guide/logs/ivanovo/{number}"
            page = client.get(path)
            assert page.status_code == 200
            assert "Запуски · 1" in page.text
            assert "Исследование · " in page.text
            assert page.text.count('class="log-request selected"') == 1
            assert client.get(path + ".json").json()["job"]["id"] == row["id"]
            assert client.get(path + "/status").json()["job"]["status"] == "done"
        assert "Запросов: 3" in client.get("/guide/logs?q=ivanovo/3").text
        assert store.groups(999, "ivanovo/3")[0]["page"] == 1


def test_independent_reassessment_is_a_new_group_and_failures_are_visible(database):
    settings, engine, uow, queries, city, jobs, queue = filled(database)
    restaurant = queries.rows("restaurants")[0]
    identity = CityContent(uow).enqueue(city["id"], "research", restaurant["id"], "assessment")
    listing, selected = AIAudit(engine, settings).groups(1, job_id=identity)
    assert listing["total"] == 2 and selected["request_count"] == 1
    assert selected["group_id"] == identity
    with uow() as work:
        work.repo.change("jobs", identity, {"status": "failed", "error": "Test failure"})
    store = AIAudit(engine, settings)
    listing, selected = store.groups(1, status="failed", job_id=identity)
    assert listing["total"] == 1 and selected["stage_label"] == "Есть ошибки"
    with TestClient(create_app(settings)) as client:
        login(client)
        number = store.detail(identity)["job"]["run_number"]
        page = client.get(f"/guide/logs/ivanovo/{number}")
        assert "Повторить новым запросом" in page.text
        assert "Test failure" in page.text


def test_grouping_never_exposes_private_members(database):
    settings, engine, uow, queries, city, *_ = filled(database)
    rows = sorted(queries.rows("jobs"), key=lambda r: r["created_at"])
    with uow() as work:
        work.repo.change("jobs", rows[-1]["id"], {"public_log": False})
    store = AIAudit(engine, settings)
    listing, _ = store.groups(1, public_only=True)
    assert listing["total"] == 1 and listing["rows"][0]["request_count"] == 2
    assert store.groups(1, public_only=False)[0]["rows"][0]["request_count"] == 3
    with TestClient(create_app(settings)) as client:
        assert client.get("/guide/logs/ivanovo/3").status_code == 404
        assert client.get("/guide/logs/ivanovo/3.json").status_code == 404


def test_pagination_counts_launches_not_children(database):
    from uuid import uuid4

    settings, engine, uow, queries, city, *_ = filled(database)
    with uow() as work:
        for _ in range(40):
            identity = str(uuid4())
            work.repo.queue(
                {
                    "id": identity,
                    "kind": "generate",
                    "location_id": city["id"],
                    "public_log": True,
                    "content_epoch": city["content_epoch"],
                    "status": "done",
                    "prompt": "Test standalone job",
                    "report": {"source": "server", "operation": "intro", "log_group": identity},
                }
            )
    store = AIAudit(engine, settings)
    first, _ = store.groups(1, public_only=True)
    last, _ = store.groups(999, public_only=True)
    assert first["total"] == 41 and first["pages"] == 2 and len(first["rows"]) == 40
    assert last["page"] == 2 and len(last["rows"]) == 1 and last["rows"][0]["request_count"] == 3
    assert len(queries.rows("jobs")) == 43


def test_old_initial_jobs_are_grouped_read_only_from_existing_metadata(database):
    settings, engine, uow, queries, city, *_ = filled(database)
    with uow() as work:
        for job in queries.rows("jobs"):
            report = dict(job["report"])
            report.pop("log_group")
            work.repo.change("jobs", job["id"], {"report": report})
    before = queries.rows("jobs")
    listing, _ = AIAudit(engine, settings).groups(1, public_only=True)
    assert listing["total"] == 1 and listing["rows"][0]["request_count"] == 3
    assert queries.rows("jobs") == before
