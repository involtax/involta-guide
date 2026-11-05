import hashlib
import json
import os
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import text

from involta_guide.infrastructure.location_catalog import load_catalog
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow, create_database
from involta_guide.infrastructure.seed import seed
from involta_guide.main import create_app
from involta_voice.contracts import OutboxEvent
from involta_voice.db import migrate_connection

from .test_editorial import services
from .test_voice_service import metadata, recording
from .test_web import login

HEADERS = {"origin": "http://testserver"}


def test_nearby_is_directed_and_settings_save_does_not_clear_it(database):
    settings, engine = database
    _, queries, _ = services(database)
    cities = {
        slug: queries.directory(query=name)["rows"][0]
        for slug, name in [
            ("kostroma", "Кострома"),
            ("kineshma", "Кинешма"),
            ("yaroslavl", "Ярославль"),
        ]
    }
    kostroma, kineshma, yaroslavl = (cities[slug] for slug in cities)
    with TestClient(create_app(settings)) as client:
        csrf = login(client)
        endpoint = "/guide/admin/locations/kostroma/nearby/"
        assert (
            client.post(
                endpoint + "add", data={"nearby_id": yaroslavl["id"]}, headers=HEADERS
            ).status_code
            == 403
        )
        for _ in range(2):
            response = client.post(
                endpoint + "add",
                data={"_csrf": csrf, "nearby_id": yaroslavl["id"]},
                headers=HEADERS,
            )
            assert response.status_code == 200
        assert queries.get("locations", kostroma["id"])["nearby_location_ids"] == [yaroslavl["id"]]
        assert queries.get("locations", yaroslavl["id"])["nearby_location_ids"] == []
        assert queries.get("locations", kineshma["id"])["nearby_location_ids"] == []
        page = client.get("/guide/kostroma").text
        assert page.index("data-public-reviews") < page.index('id="nearby-title"')
        assert 'А еще рядом <em class="serif">есть</em>' in page
        assert 'href="/guide/yaroslavl"' in page
        assert 'href="/guide/yaroslavl"' not in client.get("/guide/kineshma").text
        # Native search works without JS and never adds an ambiguous result itself.
        fallback = client.post(
            endpoint + "add", data={"_csrf": csrf, "q": "ярослав"}, headers=HEADERS
        )
        assert "nearby-search-results" in fallback.text
        from involta_guide.application.commands.schemas import LocationInput

        values = LocationInput.model_validate(kostroma).model_dump(mode="json")
        from involta_guide.application.services.locations import LocationService

        LocationService(lambda: PostgresUow(engine)).save(kostroma["id"], values)
        assert queries.get("locations", kostroma["id"])["nearby_location_ids"] == [yaroslavl["id"]]
        response = client.post(
            endpoint + "add", data={"_csrf": csrf, "nearby_id": kostroma["id"]}, headers=HEADERS
        )
        assert "не может ссылаться на себя" in response.text
        response = client.post(
            endpoint + "remove", data={"_csrf": csrf, "nearby_id": yaroslavl["id"]}, headers=HEADERS
        )
        assert response.status_code == 200
        assert queries.get("locations", kostroma["id"])["nearby_location_ids"] == []
        assert 'id="nearby-title"' not in client.get("/guide/kostroma").text


def test_git_lists_replace_db_lists_without_overwriting_content(database):
    settings, engine = database
    catalog = load_catalog(settings.root_dir)
    city, other = catalog["locations"][:2]
    with PostgresUow(engine) as work:
        work.repo.change(
            "locations",
            city["id"],
            {"nearby_location_ids": [other["id"]], "intro": "Сохранить вступление"},
        )
    seed(PostgresUow(engine), settings.root_dir)
    with PostgresUow(engine) as work:
        row = work.repo.get("locations", city["id"])
        assert row["nearby_location_ids"] == city["nearby_location_ids"]
        assert row["intro"] == "Сохранить вступление"
        assert all(type(row["id"]) is int for row in work.repo.rows("locations", limit=100000))


def test_populated_uuid_cutover_preserves_content_and_pending_voice(database, monkeypatch):
    settings, admin_engine = database
    schema, voice = "test_ids_" + uuid4().hex, "test_ids_voice_" + uuid4().hex
    url = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    monkeypatch.setenv("DATABASE_SCHEMA", schema)
    monkeypatch.setenv("VOICE_DATABASE_SCHEMA", voice)
    settings = settings.model_copy(update={"database_schema": schema})
    engine = create_database(settings)
    config = Config(str(settings.root_dir / "alembic.ini"))
    catalog = load_catalog(settings.root_dir)
    expected = next(row["id"] for row in catalog["locations"] if row["slug"] == "ivanovo")
    old = str(uuid5(NAMESPACE_URL, "https://involta.ru/guide/ivanovo"))
    parent, restaurant, review, job = (str(uuid4()) for _ in range(4))
    event_id, reference = str(uuid4()), str(uuid4())
    meta = metadata().model_dump(mode="json")
    meta["location"]["id"] = old
    event = {
        "schema_version": 2,
        "event_id": event_id,
        "sequence": 1,
        "kind": "recording_received",
        "reference": reference,
        "run": 1,
        "metadata": meta,
        "sha256": hashlib.sha256(recording()).hexdigest(),
        "mime": "audio/wav",
        "duration": 6,
        "created_at": "2026-11-05T00:00:00Z",
    }
    try:
        command.upgrade(config, "audience_stats")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO locations(id,name,region,slug) VALUES(:id,'Parent','Region','custom-parent')"
                ),
                {"id": parent},
            )
            conn.execute(
                text(
                    "INSERT INTO locations(id,name,region,slug,parent_id,intro) VALUES(:id,'Иваново','Region','renamed-ivanovo',:parent,'Живое вступление')"
                ),
                {"id": old, "parent": parent},
            )
            conn.execute(
                text(
                    "INSERT INTO restaurants(id,location_id,name,cuisine,description,maps_url) VALUES(:id,:city,'Ресторан','Кухня','Текст','https://yandex.ru/maps/')"
                ),
                {"id": restaurant, "city": old},
            )
            conn.execute(
                text(
                    "INSERT INTO reviews(id,location_id,visitor,audio,mime,hash,ip,user_agent,status) VALUES(:id,:city,'visitor','voice:old','audio/wav','sha','127.0.0.1','test','received')"
                ),
                {"id": review, "city": old},
            )
            conn.execute(
                text("INSERT INTO jobs(id,location_id,kind) VALUES(:id,:city,'generate')"),
                {"id": job, "city": old},
            )
            conn.execute(
                text(
                    "INSERT INTO audit(id,action,target,detail,location_id) VALUES(:id,'location',:target,:detail,:city)"
                ),
                {
                    "id": str(uuid4()),
                    "city": old,
                    "target": old,
                    "detail": json.dumps({"location_id": old}),
                },
            )
            conn.execute(
                text(
                    "INSERT INTO voice_inbox(event_id,sequence,run,kind,location_id,content_epoch,payload,state) VALUES(:id,1,1,'recording_received',:city,1,CAST(:event AS jsonb),'pending')"
                ),
                {"id": event_id, "city": old, "event": json.dumps(event)},
            )
            conn.exec_driver_sql(f"SET LOCAL search_path={voice}")
            migrate_connection(conn, version=9)
            conn.execute(
                text(
                    "INSERT INTO submissions(reference,location_id,visitor,idempotency_key,payload_hash,metadata,audio,mime,duration,size,sha256,repeat_signals) VALUES(:id,:city,:visitor,:key,'old-hash',CAST(:meta AS jsonb),'legacy-file.wav','audio/wav',6,100,:sha,'{}')"
                ),
                {
                    "id": reference,
                    "city": old,
                    "key": meta["idempotency_key"],
                    "visitor": meta["visitor"],
                    "meta": json.dumps(meta),
                    "sha": event["sha256"],
                },
            )
            conn.execute(
                text(
                    "INSERT INTO runs(submission,number,snapshot) VALUES(:id,1,CAST(:snapshot AS jsonb))"
                ),
                {
                    "id": reference,
                    "snapshot": json.dumps(
                        {"location": meta["location"], "transcription_model": "gigaam"}
                    ),
                },
            )
            conn.execute(
                text(
                    "INSERT INTO outbox(event_id,submission,run,kind,payload) VALUES(:id,:ref,1,'recording_received',CAST(:payload AS jsonb))"
                ),
                {"id": event_id, "ref": reference, "payload": json.dumps(event)},
            )
            conn.execute(
                text("INSERT INTO location_epochs(location_id,content_epoch) VALUES(:city,1)"),
                {"city": old},
            )
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        with engine.connect() as conn:
            city = (
                conn.execute(text("SELECT * FROM locations WHERE id=:id"), {"id": expected})
                .mappings()
                .one()
            )
            assert city["intro"] == "Живое вступление" and city["slug"] == "renamed-ivanovo"
            assert type(city["parent_id"]) is int
            for table in ("restaurants", "reviews", "jobs"):
                assert (
                    conn.execute(text(f"SELECT location_id FROM {table}")).scalar_one() == expected
                )
            assert conn.execute(text("SELECT number FROM request_logs")).scalar_one() == 1
            assert conn.execute(
                text("SELECT target FROM audit WHERE action='location'")
            ).scalar_one() == str(expected)
            OutboxEvent.model_validate(
                conn.execute(text("SELECT payload FROM voice_inbox")).scalar_one()
            )
            assert (
                conn.execute(text("SELECT to_regclass('location_relations')")).scalar_one() is None
            )
        with engine.begin() as conn:
            conn.exec_driver_sql(f"SET LOCAL search_path={voice}")
            migrate_connection(conn)
            assert (
                conn.execute(text("SELECT location_id FROM submissions")).scalar_one() == expected
            )
            frozen = conn.execute(text("SELECT snapshot FROM runs")).scalar_one()
            assert frozen["location"]["id"] == expected
            migrated = OutboxEvent.model_validate(
                conn.execute(text("SELECT payload FROM outbox")).scalar_one()
            )
            assert migrated.metadata.location.id == expected
            assert (
                conn.execute(text("SELECT audio FROM submissions")).scalar_one()
                == "legacy-file.wav"
            )
        from involta_voice.config import Settings as VoiceSettings
        from involta_voice.db import database as voice_database
        from involta_voice.service import VoiceService

        voice_settings = VoiceSettings(
            database_url=settings.database_url,
            database_schema=voice,
            service_secret="test-numeric-identities-" + "x" * 40,
            data_dir=settings.data_dir / "voice",
            min_free_bytes=0,
            fake_provider=True,
        )
        voice_engine = voice_database(voice_settings)
        try:
            repeated = VoiceService(voice_engine, voice_settings).submit(
                migrated.metadata, recording(), "audio/wav"
            )
            assert repeated["reference"] == reference
        finally:
            voice_engine.dispose()
    finally:
        engine.dispose()
        with psycopg.connect(url, autocommit=True) as conn:
            for value in (schema, voice):
                conn.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(value))
                )


def test_shared_search_adds_only_current_city_after_htmx_navigation(database):
    from pathlib import Path

    from playwright.sync_api import expect, sync_playwright

    settings, engine = database
    screenshots = Path("var/screenshots/nearby")
    screenshots.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app(settings)) as client, sync_playwright() as p:
        login(client)
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        errors = []
        requests = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def respond(route):
            request = route.request
            headers = {"origin": "http://testserver"}
            if request.headers.get("hx-request"):
                headers["HX-Request"] = "true"
            response = client.request(
                request.method,
                request.url,
                content=request.post_data_buffer,
                headers={
                    **headers,
                    "content-type": request.headers.get(
                        "content-type", "application/x-www-form-urlencoded"
                    ),
                },
            )
            requests.append((request.method, request.url, response.status_code))
            route.fulfill(
                status=response.status_code,
                body=response.content,
                content_type=response.headers.get("content-type", "text/plain"),
                headers={k: v for k, v in response.headers.items() if k.lower().startswith("hx-")},
            )

        page.route("**/*", respond)
        page.goto("http://testserver/guide/admin/locations/kostroma")
        page.locator("[data-location-search] input[name=q]").fill("ярослав")
        option = page.locator(".destination-result[role=option]").filter(has_text="Ярославль")
        expect(option).to_be_visible()
        page.locator(".location-nearby").screenshot(
            path=str(screenshots / "admin-search-desktop.png")
        )
        option.click()
        expect(page.locator(".nearby-row")).to_have_count(1)
        expect(page.locator(".nearby-row")).to_contain_text("Ярославль")
        # Search must mount after the outerHTML swap as well.
        page.locator("[data-location-search] input[name=q]").fill("плёс")
        page.locator(".destination-result[role=option]").filter(has_text="Плёс").click()
        expect(page.locator(".nearby-row")).to_have_count(2)
        page.locator(".location-nearby").screenshot(path=str(screenshots / "admin-desktop.png"))
        for width in (768, 390, 360):
            page.set_viewport_size({"width": width, "height": 1000})
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            if width == 390:
                page.locator(".location-nearby").screenshot(
                    path=str(screenshots / "admin-mobile.png")
                )
        page.goto("http://testserver/guide/kostroma")
        expect(page.locator(".nearby-locations .city-link")).to_have_count(2)
        page.locator(".nearby-locations").screenshot(path=str(screenshots / "public-mobile.png"))
        page.set_viewport_size({"width": 1440, "height": 1100})
        page.locator(".nearby-locations").screenshot(path=str(screenshots / "public-desktop.png"))
        page.goto("http://testserver/guide/admin/locations/kineshma")
        expect(page.locator(".nearby-row")).to_have_count(0)
        page.goto("http://testserver/guide/admin/locations/kostroma")
        page.get_by_role("button", name="Убрать Ярославль из списка").click()
        expect(page.locator(".nearby-row")).to_have_count(1)
        assert not errors, errors
        browser.close()
