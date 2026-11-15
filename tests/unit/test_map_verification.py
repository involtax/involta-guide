import json

import httpx
import pytest

from involta_guide.catalogue.schemas import parse_result
from involta_guide.infrastructure import maps


def business_html(identity="123", title="Ресторан", address="Город, улица, 1"):
    state = {
        "config": {"query": {"orgpage": {"id": identity}}},
        "stack": [
            {
                "results": {
                    "items": [
                        {"type": "business", "id": identity, "title": title, "address": address}
                    ]
                }
            }
        ],
    }
    return '<script type="application/json" class="state-view">' + json.dumps(state) + "</script>"


def use_transport(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(
        maps.httpx,
        "Client",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)),
    )


def test_verified_replacement_follows_only_same_organization_and_deduplicates(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        assert "authorization" not in request.headers
        if request.url.host == "yandex.ru":
            return httpx.Response(
                302, headers={"location": "https://yandex.com/maps/org/cafe/123/"}
            )
        return httpx.Response(200, headers={"content-type": "text/html"}, text=business_html())

    use_transport(monkeypatch, handler)
    first = "https://yandex.future-region.test/maps/org/name/123/?tracking=ignored"
    second = "https://yandex.uz/maps/ru/org/name/123/reviews/"
    assert maps.verified_yandex_maps([first, first, second]) == {
        first: "https://yandex.ru/maps/org/123/",
        second: "https://yandex.ru/maps/org/123/",
    }
    assert seen == ["https://yandex.ru/maps/org/123/", "https://yandex.com/maps/org/cafe/123/"]


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.test/maps/org/123/",
        "https://127.0.0.1/maps/org/123/",
        "https://yandex.com.evil.test/maps/org/123/",
        "https://user:secret@yandex.com/maps/org/123/",
        "http://yandex.com/maps/org/123/",
        "https://yandex.com/maps/org/456/",
        "https://yandex.com/showcaptcha",
        "https://yandex.com:9000/maps/org/123/",
    ],
)
def test_bad_redirect_is_not_followed(monkeypatch, target):
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(302, headers={"location": target})

    use_transport(monkeypatch, handler)
    assert maps.verified_yandex_maps(["https://yandex.uz/maps/org/123/"]) == {}
    assert len(seen) == 1 and seen[0].host == "yandex.ru"


@pytest.mark.parametrize(
    "status,body",
    [
        (404, business_html()),
        (429, business_html()),
        (200, "<title>Captcha</title>"),
        (200, business_html("999")),
        (200, business_html(title="")),
        (200, business_html(address="")),
        (200, '<script type="application/json" class="state-view">not json</script>'),
        (200, "x" * (maps.MAX_BYTES + 1)),
    ],
)
def test_http_200_alone_does_not_verify_a_card(monkeypatch, status, body):
    use_transport(
        monkeypatch,
        lambda request: httpx.Response(status, headers={"content-type": "text/html"}, text=body),
    )
    assert maps.verified_yandex_maps(["https://yandex.uz/maps/org/123/"]) == {}


def test_verification_has_a_batch_limit(monkeypatch):
    seen = []

    def handler(request):
        identity = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        seen.append(identity)
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text=business_html(identity)
        )

    use_transport(monkeypatch, handler)
    duplicate = "https://yandex.com.ge/maps/org/0/"
    result = maps.verified_yandex_maps(
        [f"https://yandex.uz/maps/org/{i}/" for i in range(50)] + [duplicate]
    )
    assert len(seen) == maps.MAX_CARDS
    assert len(result) == maps.MAX_CARDS + 1
    assert result[duplicate] == "https://yandex.ru/maps/org/0/"


def test_unrecognized_domain_gets_budget_before_optional_normalization(monkeypatch):
    seen = []

    def handler(request):
        identity = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        seen.append(identity)
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text=business_html(identity)
        )

    use_transport(monkeypatch, handler)
    original = "https://yandex.unknown-region.test/maps/org/123/"
    urls = [f"https://yandex.uz/maps/org/{i}/" for i in range(20)] + [original]
    assert maps.verified_yandex_maps(urls)[original] == "https://yandex.ru/maps/org/123/"
    assert seen[0] == "123"


def test_expired_budget_does_not_make_a_request(monkeypatch):
    ticks = iter([0.0, maps.BUDGET_SECONDS + 1])
    monkeypatch.setattr(maps.time, "monotonic", lambda: next(ticks))

    def handler(request):
        pytest.fail("The batch deadline has expired")

    use_transport(monkeypatch, handler)
    assert maps.verified_yandex_maps(["https://yandex.uz/maps/org/123/"]) == {}


def test_timeout_keeps_allowed_original_map_and_drops_unsupported_one(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("Fixture timeout", request=request)

    use_transport(monkeypatch, handler)
    raw = {
        "restaurants": [
            {
                "name": "A",
                "mapsUrl": "https://yandex.uz/maps/org/123/",
                "sources": ["https://example.test/a"],
                "reason": "A",
            },
            {
                "name": "B",
                "mapsUrl": "https://yandex.future-region.test/maps/org/456/",
                "sources": ["https://example.test/b"],
                "reason": "B",
            },
        ]
    }
    payload, warnings = parse_result("discover", raw, maps.verified_yandex_maps)
    assert payload["restaurants"][0]["mapsUrl"] == raw["restaurants"][0]["mapsUrl"]
    assert payload["restaurants"][1]["mapsUrl"] is None
    assert len(warnings) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/maps/org/123/",
        "https://fake-yandex.uz/maps/org/123/",
        "https://yandex.uz/maps/?text=restaurant",
        "https://user:secret@yandex.uz/maps/org/123/",
        "http://yandex.uz/maps/org/123/",
        "https://[malformed/",
        "https://yandex.ru/maps/org/123/",
    ],
)
def test_no_network_for_urls_without_recoverable_regional_organization(monkeypatch, url):
    def handler(request):
        pytest.fail("This URL must not start any network request")

    use_transport(monkeypatch, handler)
    assert maps.verified_yandex_maps([url]) == {}
