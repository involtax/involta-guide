import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import text

from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_guide.main import create_app

from .test_hybrid_search import public_restaurant
from .test_location_search import add_location
from .test_voice_upload_transport import serving

HEADERS = {"accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-03-26"}


def rpc(client, method, params=None):
    response = client.post(
        "/guide/mcp",
        headers=HEADERS,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex"
    return response.json()


def call(client, name, arguments):
    result = rpc(client, "tools/call", {"name": name, "arguments": arguments})["result"]
    assert not result.get("isError"), result
    structured = result["structuredContent"]
    assert json.loads(result["content"][0]["text"]) == structured
    return structured


def test_mcp_transport_survives_restarting_the_same_application(database):
    settings, _ = database
    app = create_app(settings)
    for _ in range(2):
        with TestClient(app) as client:
            tools = rpc(client, "tools/list")["result"]["tools"]
            assert {tool["name"] for tool in tools} == {"Location", "Restaurant"}
            result = call(client, "Location", {"location": "ivanovo"})
            assert result["location"]["location"] == "ivanovo"


def snapshot(engine):
    with engine.connect() as conn:
        return {
            table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in ("jobs", "reviews", "audit", "search_attempts", "audience_daily")
        }


def review(engine, location_id, number, *, publication=1, status="transcribed", transcript=None):
    identity = str(uuid4())
    with PostgresUow(engine) as work:
        work.repo.add(
            "reviews",
            {
                "id": identity,
                "location_id": location_id,
                "visitor": "private-visitor-value",
                "audio": "private-audio-path",
                "mime": "audio/wav",
                "hash": identity,
                "ip": "192.0.2.123",
                "user_agent": "private-user-agent",
                "content_epoch": 1,
                "status": status,
                "moderation": "approved",
                "publication_status": publication,
                "transcript": transcript,
                "created_at": datetime(2026, 11, 1, tzinfo=UTC) + timedelta(minutes=number),
            },
        )
    from .voice_fixture import classified

    if status == "transcribed":
        classified(engine, identity)
    return identity


def test_two_read_only_tools_schema_handshake_and_public_cards(database):
    settings, engine = database
    city, card = public_restaurant(settings, engine)
    before = snapshot(engine)
    with TestClient(create_app(settings)) as client:
        initialized = rpc(
            client,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "guide-test", "version": "1"},
            },
        )["result"]
        assert initialized["serverInfo"]["name"] == "involta-guide"
        assert "Location" in initialized["instructions"]
        tools = rpc(client, "tools/list")["result"]["tools"]
        assert {tool["name"] for tool in tools} == {"Location", "Restaurant"}
        for tool in tools:
            assert tool["annotations"]["readOnlyHint"]
            assert not tool["annotations"]["destructiveHint"]
            assert tool["annotations"]["idempotentHint"]
            assert tool["description"] and tool["outputSchema"]
        location = call(client, "Location", {"location": "  ИВАНОВО  "})
        assert location["status"] == "found"
        assert location["location"]["location"] == "ivanovo"
        assert location["location"]["content_status"] == "published"
        restaurant = location["location"]["restaurants"][0]
        assert restaurant["id"] == str(card["id"])
        assert restaurant["url"] == settings.url("/ivanovo#restaurant-" + str(card["id"]))
        assert restaurant["opening_hours"] is None
        assert restaurant["reviews"] == {
            "items": [],
            "total": 0,
            "limit": 10,
            "status": "not_linked",
        }
        assert (
            call(client, "Restaurant", {"restaurant_id": str(card["id"])})["restaurant"]
            == restaurant
        )
        assert call(client, "Location", {"location": "ivanovo"}) == location
        assert call(client, "Location", {"location": "Иван"})["status"] == "not_found"
        assert call(client, "Restaurant", {"restaurant_id": str(uuid4())})["status"] == "not_found"
        raw = json.dumps(location)
        assert all(
            value not in raw
            for value in ("ranking", "content_epoch", "reset_pending", "alex_star", "revision")
        )
    assert snapshot(engine) == before


def test_location_reviews_are_public_latest_texts_and_not_attributed_to_restaurants(database):
    settings, engine = database
    city, card = public_restaurant(settings, engine)
    ids = [review(engine, city["id"], n, transcript=f"Отзыв {n}") for n in range(12)]
    review(engine, city["id"], 20, publication=2, transcript="Скрытый")
    review(engine, city["id"], 21, publication=0, transcript="Ошибка")
    review(engine, city["id"], 22, status="deleted", transcript="Удалённый")
    review(engine, city["id"], 23, status="received")
    review(engine, city["id"], 24, transcript="  ")
    with TestClient(create_app(settings)) as client:
        response = call(client, "Location", {"location": "ivanovo"})
        reviews = response["location"]["reviews"]
        assert reviews["total"] == 12
        assert [item["id"] for item in reviews["items"]] == ids[::-1][:10]
        assert [item["text"] for item in reviews["items"]] == [
            f"Отзыв {n}" for n in range(11, 1, -1)
        ]
        raw = json.dumps(response, ensure_ascii=False)
        assert all(
            secret not in raw
            for secret in (
                "private-visitor-value",
                "private-audio-path",
                "192.0.2.123",
                "private-user-agent",
                "Удалённый",
                "Скрытый",
            )
        )
        assert (
            call(client, "Restaurant", {"restaurant_id": str(card["id"])})["restaurant"]["reviews"][
                "items"
            ]
            == []
        )


def test_exact_location_ambiguity_public_visibility_and_unfilled_state(database):
    settings, engine = database
    one = add_location(engine, "Одинаково", "mcp-one", region="Первая область")
    add_location(engine, "Одинаково", "mcp-two", region="Вторая область")
    add_location(engine, "Секретно", "mcp-hidden", listed=False)
    add_location(engine, "Выключено", "mcp-inactive", active=0, ready=True)
    add_location(engine, "Плёс тест", "mcp-yo")
    with TestClient(create_app(settings)) as client:
        result = call(client, "Location", {"location": "Одинаково"})
        assert result["status"] == "ambiguous" and result["location"] is None
        assert {item["location"] for item in result["alternatives"]} == {"mcp-one", "mcp-two"}
        result = call(client, "Location", {"location": "mcp-one"})["location"]
        assert result["id"] == one and result["content_status"] == "not_filled"
        assert result["restaurants"] == []
        assert call(client, "Location", {"location": "ПЛЕС ТЕСТ"})["status"] == "found"
        for query in (
            "mcp-hidden",
            "Секретно",
            "mcp-inactive",
            "в Иванове",
            "Одинаков",
            "' OR true --",
        ):
            assert call(client, "Location", {"location": query})["status"] == "not_found"


@pytest.mark.parametrize(
    "change",
    ["r.active=0", "l.active=0", "l.reset_pending=true", "l.initialized_at=NULL"],
)
def test_unpublished_restaurant_is_not_available_even_by_known_id(database, change):
    settings, engine = database
    city, card = public_restaurant(settings, engine)
    alias, assignment = change.split(".", 1)
    table = "restaurants" if alias == "r" else "locations"
    with engine.begin() as conn:
        conn.execute(
            text(f"UPDATE {table} SET {assignment} WHERE id=:id"),
            {"id": str(card["id"]) if alias == "r" else city["id"]},
        )
    with TestClient(create_app(settings)) as client:
        assert (
            call(client, "Restaurant", {"restaurant_id": str(card["id"])})["status"] == "not_found"
        )
        location = call(client, "Location", {"location": "ivanovo"})
        assert location["status"] == "not_found" or location["location"]["restaurants"] == []


def test_mcp_rejects_invalid_input_origin_host_and_large_requests(database):
    settings, _ = database
    with TestClient(create_app(settings)) as client:
        for tool, arguments in [
            ("Restaurant", {"restaurant_id": "ivanovo"}),
            ("Location", {"location": " "}),
            ("Location", {"location": "a" * 201}),
        ]:
            assert rpc(client, "tools/call", {"name": tool, "arguments": arguments})["result"][
                "isError"
            ]
        assert rpc(client, "tools/call", {"name": "Search", "arguments": {}})["result"]["isError"]
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert (
            client.post(
                "/guide/mcp", json=body, headers={**HEADERS, "origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/guide/mcp", json=body, headers={**HEADERS, "host": "evil.test"}
            ).status_code
            == 421
        )
        assert (
            client.post(
                "/guide/mcp", json={**body, "padding": "x" * 17000}, headers=HEADERS
            ).status_code
            == 413
        )


def test_discovery_documents_sitemap_and_custom_base_path(database):
    settings, engine = database
    before = snapshot(engine)
    with TestClient(create_app(settings)) as client:
        doc = client.get("/guide/for-agents")
        assert doc.status_code == 200 and settings.url("/mcp") in doc.text
        assert 'content="index, follow"' in doc.text
        assert settings.url("/for-agents") in client.get("/guide/sitemap.xml").text
        llms = client.get("/guide/llms.txt")
        assert llms.status_code == 200 and "Location" in llms.text and "Restaurant" in llms.text
        assert client.get("/guide/for-agents.md").text == llms.text
        manifest = client.get("/guide/mcp-server.json").json()
        assert manifest["remotes"] == [{"type": "streamable-http", "url": settings.url("/mcp")}]
        assert manifest["name"] == "io.github.involtax/involta-guide"
        assert 'rel="help"' in doc.text and settings.url("/llms.txt") in doc.text
        assert "set-cookie" not in llms.headers
    assert snapshot(engine) == before
    other = settings.model_copy(update={"base_path": "/food", "site_url": "http://testserver/food"})
    with TestClient(create_app(other)) as client:
        assert "http://testserver/food/mcp" in client.get("/food/llms.txt").text
        response = client.post(
            "/food/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        assert {tool["name"] for tool in response.json()["result"]["tools"]} == {
            "Location",
            "Restaurant",
        }


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_official_sdk_over_real_http_discovers_and_calls_without_credentials(database, mode):
    settings, engine = database
    _, card = public_restaurant(settings, engine)

    async def connect(url):
        async with Client(url, mode=mode, read_timeout_seconds=10) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {"Location", "Restaurant"}
            location = await client.call_tool("Location", {"location": "Иваново"})
            assert not location.is_error
            assert location.structured_content["location"]["restaurants"][0]["id"] == str(
                card["id"]
            )
            restaurant = await client.call_tool("Restaurant", {"restaurant_id": str(card["id"])})
            assert not restaurant.is_error and restaurant.structured_content["restaurant"][
                "id"
            ] == str(card["id"])

    with serving(create_app(settings)) as origin:
        asyncio.run(connect(origin + "/guide/mcp"))
