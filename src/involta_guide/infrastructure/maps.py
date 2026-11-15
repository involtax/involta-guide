"""Verify regional map replacements against Yandex, never the untrusted source host."""

import json
import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from involta_guide.application.commands.schemas import RestaurantInput

BUDGET_SECONDS = 8.0
MAX_CARDS = 8
MAX_BYTES = 2 * 1024 * 1024
ORG_PATH = re.compile(r"^/maps/(?:[a-z]{2}/)?org/(?:[^/]+/)?([0-9]{1,20})(?:/|$)")


def map_candidate(value: str) -> tuple[str, str] | None:
    """A Yandex-looking host is only a hint; we never connect to it."""
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.username is not None
            or url.password is not None
            or url.port not in (None, 443)
            or not (url.hostname or "").startswith("yandex.")
            or url.hostname == "yandex.ru"
            or any(ord(c) <= 32 or c == "\\" for c in value)
        ):
            return None
        match = ORG_PATH.match(url.path)
        if not match:
            return None
        identity = match.group(1)
        return f"https://yandex.ru/maps/org/{identity}/", identity
    except ValueError:
        return None


class MapPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.capture = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script":
            self.capture = (
                values.get("type") == "application/json"
                and "state-view" in (values.get("class") or "").split()
            )

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.capture = False

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.parts.append(data)


def has_business(body: str, identity: str) -> bool:
    parser = MapPage()
    try:
        parser.feed(body)
        state: Any = json.loads("".join(parser.parts))
        if state["config"]["query"]["orgpage"]["id"] != identity:
            return False
        item = state["stack"][0]["results"]["items"][0]
        return bool(
            item["type"] == "business"
            and item["id"] == identity
            and item.get("title")
            and item.get("address")
        )
    except (ValueError, KeyError, TypeError, IndexError, RecursionError):
        return False


def _verified(client: httpx.Client, canonical: str, identity: str, deadline: float) -> bool:
    url = canonical
    for _ in range(4):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        # Read/connect timeouts plus the batch deadline bound slow or chunked responses.
        with client.stream("GET", url, timeout=min(2.0, remaining)) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                target = urljoin(url, response.headers.get("location", ""))
                parsed = urlsplit(target)
                match = ORG_PATH.match(parsed.path)
                if (
                    parsed.scheme != "https"
                    or parsed.hostname not in ("yandex.ru", "yandex.com")
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.port not in (None, 443)
                    or not match
                    or match.group(1) != identity
                ):
                    return False
                url = target
                continue
            if response.status_code != 200 or "text/html" not in response.headers.get(
                "content-type", ""
            ):
                return False
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_BYTES or time.monotonic() >= deadline:
                    return False
            return has_business(data.decode("utf-8", errors="replace"), identity)
    return False


def verified_yandex_maps(urls: list[str]) -> dict[str, str]:
    candidates = {
        url: candidate for url in dict.fromkeys(urls) if (candidate := map_candidate(url))
    }
    if not candidates:
        return {}

    def already_allowed(item: tuple[str, tuple[str, str]]) -> bool:
        try:
            RestaurantInput.maps(item[0])
            return True
        except ValueError:
            return False

    resolved: dict[str, str] = {}
    checked: dict[str, bool] = {}
    deadline = time.monotonic() + BUDGET_SECONDS
    with httpx.Client(follow_redirects=False, trust_env=True) as client:
        # Spend the bounded budget on otherwise lost maps before cosmetic normalization.
        for original, (canonical, identity) in sorted(candidates.items(), key=already_allowed):
            if canonical not in checked:
                if len(checked) >= MAX_CARDS or time.monotonic() >= deadline:
                    continue
                try:
                    checked[canonical] = _verified(client, canonical, identity, deadline)
                except (httpx.HTTPError, ValueError):
                    checked[canonical] = False
            if checked[canonical]:
                resolved[original] = canonical
    return resolved
