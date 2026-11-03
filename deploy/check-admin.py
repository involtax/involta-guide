"""Read-only production smoke test. Credentials/cookies never leave this process."""

import http.cookiejar
import os
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


class Form(HTMLParser):
    def __init__(self):
        super().__init__()
        self.csrf = ""
        self.login = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and values.get("name") == "_csrf":
            self.csrf = values.get("value", "")
        if tag == "input" and values.get("type") == "password":
            self.login = True


def main():
    base = os.environ["PUBLIC_URL"].rstrip("/")
    assert base == "https://involta.ru/guide", "Unexpected production origin"
    password = os.environ.get("ADMIN_PASSWORD", "")
    assert password, "Set ADMIN_PASSWORD to test authenticated pages"
    origin = urlsplit(base)
    origin = f"{origin.scheme}://{origin.netloc}"
    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, fields=None):
        req = Request(
            base + path,
            data=urlencode(fields).encode() if fields is not None else None,
            headers={"Origin": origin, "Cache-Control": "no-cache"},
        )
        with opener.open(req, timeout=30) as response:
            assert response.status == 200, f"HTTP failure: {path}"
            form = Form()
            form.feed(response.read().decode())
            return form

    login = request("/admin")
    assert login.login and login.csrf, "Login form unavailable"
    admin = request("/admin/login", {"_csrf": login.csrf, "password": password})
    assert not admin.login and admin.csrf, "Admin login failed"
    try:
        for path in (
            "/admin",
            "/admin/locations",
            "/admin/locations/ivanovo",
            "/admin/restaurants?location=ivanovo",
            "/admin/reviews",
            "/admin/settings/filling",
            "/admin/sessions",
            "/logs",
        ):
            page = request(path)
            assert not page.login and page.csrf, f"Authenticated page unavailable: {path}"
            print(f"PASS authenticated GET {path}", flush=True)
    finally:
        request("/admin/logout", {"_csrf": admin.csrf})
    assert request("/admin").login, "Smoke session was not revoked"
    print("PASS logout; no application data changed")


if __name__ == "__main__":
    main()
