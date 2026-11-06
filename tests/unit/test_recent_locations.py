from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import Response

from involta_guide.web.http import RECENT_LOCATIONS_COOKIE, remember_location


def remember(value, location_id=34):
    request = Request(
        {
            "type": "http",
            "headers": [(b"cookie", f"{RECENT_LOCATIONS_COOKIE}={value}".encode())],
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    context=SimpleNamespace(
                        settings=SimpleNamespace(base_path="/guide", cookie_secure=True)
                    )
                )
            ),
        }
    )
    response = Response()
    remember_location(request, response, location_id)
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie[RECENT_LOCATIONS_COOKIE]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", "34"),
        ("622.34.467", "34.622.467"),
        ("622.622.34.467.467", "34.622.467"),
        ("0.-1.bad.3.2147483648.0003", "34.3"),
        ("9" * 220, "34"),
    ],
)
def test_history_recovers_invalid_values_and_moves_repeat_to_front(value, expected):
    assert remember(value).value == expected


def test_cookie_is_bounded_persistent_and_scoped_to_guide():
    cookie = remember(".".join(str(key) for key in range(1, 21)), 21)
    assert cookie.value == ".".join(str(key) for key in [21, *range(1, 20)])
    assert cookie["max-age"] == str(90 * 86400)
    assert cookie["path"] == "/guide"
    assert cookie["httponly"] and cookie["secure"]
    assert cookie["samesite"] == "lax" and cookie["domain"] == ""
    assert len(remember(".".join(str(2147483647 - key) for key in range(20))).value) <= 219
