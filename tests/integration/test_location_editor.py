from fastapi.testclient import TestClient

from involta_guide.main import create_app

from .test_editorial import CONTENT, services
from .test_web import login


def test_city_shows_current_data_and_only_supported_edit_channels(database):
    settings, _ = database
    editor, queries, _ = services(database)
    city = queries.directory(query="Иваново")["rows"][0]
    editor.save(city["id"], CONTENT, None)
    with TestClient(create_app(settings)) as client:
        csrf = login(client)
        page = client.get("/guide/admin/locations/" + city["slug"])
        assert page.status_code == 200
        assert CONTENT["intro"] in page.text
        assert "Наполнить через GPT" not in page.text
        assert "Сохранить вступление" in page.text
        assert "Восстановить" not in page.text
        assert "Сохранённые версии" not in client.get("/guide/admin/restaurants").text
        assert client.get("/guide/logs/history/" + str(city["id"])).status_code in (404, 422)
        for action in ("intro", "content", "restore", "publish", "select-restaurant"):
            assert (
                client.post(
                    "/guide/admin/actions/" + action,
                    data={"_csrf": csrf, "location_id": city["id"]},
                    headers={"origin": "http://testserver"},
                ).status_code
                == 404
            )
        assert client.get("/guide/admin/locations/new").status_code == 200
        assert client.get("/guide/admin/locations?q=ничего").status_code == 200


def test_current_schema_has_no_page_history(database):
    from sqlalchemy import inspect

    _, engine = database
    inspector = inspect(engine)
    assert not {
        "versions",
        "version_reviews",
        "voice_page_drafts",
        "voice_candidate_proposals",
        "schema_migrations",
    } & set(inspector.get_table_names())
    columns = {c["name"] for c in inspector.get_columns("locations")}
    assert {"initialized_at", "content_epoch", "revision", "updated_at"} <= columns
    assert "page_data" not in columns
    assert not {"draft", "draft_version", "published_version"} & columns
    assert "alex_changes" not in inspector.get_table_names()
