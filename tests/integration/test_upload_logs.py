import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.infrastructure.voice.upload_debug import UploadDebug
from involta_guide.infrastructure.voice.upload_logs import UploadLogs
from involta_guide.main import create_app

from .test_jobs import setup
from .test_web import login

NGINX = "<html><head><title>504 Gateway Time-out</title></head><body>nginx/1.28.3</body></html>"


def upload(database, *, outcome="browser", request_id=None):
    settings, engine, _, _, city, _, _ = setup(database)
    request_id = request_id or str(uuid4())
    store = UploadDebug(engine, settings)
    started = datetime.now(UTC) - timedelta(seconds=65)

    def record(event):
        store.append(
            request_id,
            {
                "_context": {"slug": city["slug"], "content_epoch": city["content_epoch"]},
                "owner": "private-owner",
                **event,
            },
        )
        # A coherent historical fixture: browser and server clocks describe
        # the same attempt, rather than mixing a fixed date with insertion time.
        offset = {"received": 0.7, "body_received": 0.9, "response": 1.0, "browser": 61.0}
        at = started + timedelta(seconds=offset[event["stage"]])
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE audit SET created_at=:at,detail="
                    "jsonb_set(detail::jsonb,'{time}',to_jsonb(CAST(:iso AS text)))::text "
                    "WHERE target=:id AND detail::jsonb->>'stage'=:stage"
                ),
                {"id": request_id, "stage": event["stage"], "at": at, "iso": at.isoformat()},
            )

    record(
        {
            "stage": "received",
            "path": "/guide/voice/api/v1/submissions",
            "content_length": "326669",
            "content_type": "audio/webm;codecs=opus",
        }
    )
    if outcome in ("done", "rejected"):
        record({"stage": "body_received", "received_bytes": 326669})
        record(
            {
                "stage": "response",
                "status": 202 if outcome == "done" else 422,
                "received_bytes": 326669,
                "elapsed_ms": 225,
                "response_body": json.dumps(
                    {
                        "state": "accepted",
                        "transcript": "private-review",
                        "future_field": "private-future-payload",
                        "metadata": {"visitor": "private-visitor"},
                    }
                ),
            }
        )
    if outcome in ("browser", "done", "network"):
        record(
            {
                "stage": "browser",
                "visitor": "private-visitor",
                "report": {
                    "version": 2,
                    "browser": "private-agent",
                    "role": "admin",
                    "draft": {
                        "id": "private-idempotency-key",
                        "bytes": 326669,
                        "durationMs": 20280,
                        "mime": "audio/webm;codecs=opus",
                        "content_epoch": 1,
                        "local_copy_saved": True,
                    },
                    "events": [
                        {
                            "url": "https://testserver/guide/voice/api/v1/submissions?idempotency_key=private-key",
                            "method": "POST",
                            "started": started.isoformat(),
                            "elapsed_ms": 60623,
                            **(
                                {"status": 504, "response_body": NGINX}
                                if outcome != "network"
                                else {}
                            ),
                            "exception": {"name": "Error", "message": "Сбой отправки"},
                            "response_headers": {
                                "content-type": "text/html",
                                "x-visitor": "private-header",
                            },
                        }
                    ],
                    "timeline": [
                        {"name": "blob_ready", "ms": 20551, "private": "private-timeline"}
                    ],
                },
            }
        )
    return settings, engine, city, request_id


def test_existing_upload_is_visible_in_common_logs_without_private_metadata(database):
    settings, engine, city, request_id = upload(database)
    path = f"/guide/logs/ivanovo/uploads/{request_id}"
    with engine.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM audit")).scalar_one()
    with TestClient(create_app(settings)) as client:
        listing = client.get("/guide/logs?kind=uploads")
        assert listing.status_code == 200, listing.text
        assert "Загрузки голоса" in listing.text and path in listing.text
        assert "Браузер получил HTTP 504" in listing.text
        assert 'name="kind" value="uploads"' in listing.text
        assert "Диагностика записи" not in listing.text
        page = client.get(path)
        assert page.status_code == 200
        assert "20.28 с" in page.text and "326669 байт" in page.text
        assert "60623 мс" in page.text and "приёма аудио и ответ Guide" in page.text
        assert "&lt;title&gt;504 Gateway Time-out" in page.text
        assert "<html><head><title>504" not in page.text
        assert 'href="/guide/ivanovo"' in page.text
        exported = client.get(path + ".json")
        assert exported.status_code == 200, exported.text
        detail = exported.json()
        assert detail["request"]["request_id"] == request_id
        assert detail["request"]["status"] == "failed"
        assert len(detail["timeline"]) == 4
        assert detail["facts"]["duration_ms"] == 20280
        for response in (listing, page, exported):
            assert "private-" not in response.text
            assert "noindex" in response.headers["x-robots-tag"]
            assert "no-store" in response.headers["cache-control"]
            assert response.headers["vary"] == "Cookie"
        moved = client.get(path.replace("ivanovo", "old-name"), follow_redirects=False)
        assert moved.status_code == 308 and moved.headers["location"] == path
        assert client.get(f"/guide/logs/ivanovo/uploads/{uuid4()}").status_code == 404
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM audit")).scalar_one() == before


def test_admin_sees_full_report_but_successful_text_stays_private_on_public_log(database):
    settings, engine, _, request_id = upload(database, outcome="done")
    path = f"/guide/logs/ivanovo/uploads/{request_id}"
    with TestClient(create_app(settings)) as client:
        public = client.get(path + ".json")
        assert "private-review" not in public.text
        assert public.json()["request"]["status"] == "done"
        assert "Браузер сообщил об ошибке" in public.json()["explanation"]
        assert "Guide полностью получил аудио" in client.get(path).text
        login(client)
        admin = client.get(path + ".json?download=1")
        assert "private-review" in admin.text and "private-agent" in admin.text
        assert "attachment" in admin.headers["content-disposition"]
        page = client.get(path)
        assert 'aria-label="Администрирование"' in page.text
        assert "Принято" in page.text
    disabled = settings.model_copy(update={"public_fill_logs_enabled": False})
    with TestClient(create_app(disabled)) as client:
        assert client.get("/guide/logs?kind=uploads").status_code == 404
        assert client.get(path).status_code == 404
        assert client.get(path + ".json").status_code == 404
        login(client)
        assert client.get(path).status_code == 200


def test_upload_search_status_pagination_and_missing_finish(database):
    settings, engine, city, accepted = upload(database, outcome="done")
    _, _, _, failed = upload(database, outcome="rejected")
    _, _, _, network = upload(database, outcome="network")
    _, _, _, stalled = upload(database, outcome="received")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE audit SET created_at=now()-interval '10 minutes' WHERE target=:id"),
            {"id": stalled},
        )
    store = UploadLogs(engine, settings)
    assert store.listing(q="Иваново")["total"] == 4
    assert store.listing(q=failed)["rows"][0]["request_id"] == failed
    assert store.listing(status="done")["rows"][0]["request_id"] == accepted
    assert store.listing(status="failed")["total"] == 2
    assert store.detail(network)["request"]["status"] == "failed"
    assert store.listing(status="stalled")["rows"][0]["request_id"] == stalled
    assert store.listing(q="does-not-exist")["rows"] == []
    store_debug = UploadDebug(engine, settings)
    for _ in range(19):
        store_debug.append(
            str(uuid4()),
            {
                "stage": "received",
                "_context": {"slug": city["slug"], "content_epoch": 1},
            },
        )
    first, second = store.listing(1), store.listing(2)
    assert first["total"] == 23 and first["pages"] == 2
    assert len(first["rows"]) == 20 and len(second["rows"]) == 3
    assert not {r["request_id"] for r in first["rows"]} & {r["request_id"] for r in second["rows"]}
    assert store.listing(999)["page"] == 2
    with TestClient(create_app(settings)) as client:
        page = client.get("/guide/logs?kind=uploads&q=Иваново&status=failed")
        assert page.status_code == 200 and "Отправки · 2" in page.text
        assert "kind=uploads" in page.text and 'name="q" value="Иваново"' in page.text


def test_browser_report_with_unexpected_shapes_does_not_break_log(database):
    settings, engine, city, request_id = upload(database, outcome="received")
    UploadDebug(engine, settings).append(
        request_id,
        {
            "stage": "browser",
            "_context": {"slug": city["slug"], "content_epoch": 1},
            "report": {
                "events": [
                    None,
                    "unexpected",
                    {"elapsed_ms": "NaN", "started": "bad-date"},
                    {"elapsed_ms": 60623, "started": "9999-12-31T23:59:59Z"},
                ],
                "draft": {"durationMs": float("nan"), "bytes": []},
                "timeline": "not-an-array",
            },
        },
    )
    with TestClient(create_app(settings)) as client:
        response = client.get(f"/guide/logs/ivanovo/uploads/{request_id}")
        assert response.status_code == 200, response.text
        assert "Длительность неизвестна" in response.text
