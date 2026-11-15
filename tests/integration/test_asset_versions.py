from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from involta_guide.main import create_app


class PageAssets(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.urls = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.urls.append(attrs["src"])
        elif tag == "link" and attrs.get("rel") == "stylesheet":
            self.urls.append(attrs["href"])


def test_pages_bind_styles_and_scripts_to_the_release_and_serve_them(database):
    settings, _ = database
    sha = "a" * 40
    settings = settings.model_copy(update={"release_sha": sha})
    seen = set()
    with TestClient(create_app(settings)) as client:
        for path in ("/", "/ivanovo", "/admin", "/for-agents", "/logs/ivanovo"):
            response = client.get(settings.path(path))
            assert response.status_code == 200, path
            assets = PageAssets(response.text).urls
            assert assets, path
            for url in assets:
                parsed = urlsplit(url)
                assert parse_qs(parsed.query) == {"v": [sha]}, (path, url)
                if url not in seen:
                    resource = client.get(url)
                    assert resource.status_code == 200, url
                    seen.add(url)
        assert settings.asset("/assets/panorama.css") in seen
        assert settings.asset("/assets/panorama.js") in seen
        assert settings.asset("/assets/admin.css") in seen
        assert settings.asset("/voice/widget.v1.js") in seen


def test_deploy_and_rollback_use_distinct_urls_without_changing_routes(database):
    settings, _ = database
    versions = []
    for sha in ("a" * 40, "b" * 40, "a" * 40):
        release = settings.model_copy(
            update={
                "release_sha": sha,
                "base_path": "/preview/guide",
                "site_url": "http://testserver/preview/guide",
            }
        )
        assert release.path("/ivanovo") == "/preview/guide/ivanovo"
        versions.append(release.asset("/assets/panorama.css"))
    assert versions[0] != versions[1]
    assert versions[0] == versions[2]
    assert versions[0].startswith("/preview/guide/assets/panorama.css?v=")
