from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.main import create_app

from .test_web import login


class Metadata(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            attrs = dict(attrs)
            self.values[attrs.get("property") or attrs.get("name")] = attrs.get("content")


MEDVEZHEGORSK_INTRO = (
    "Особо негде, но пельмени в ларьке у дороги могут спасти если случай экстренный."
)
SEO_DESCRIPTION = (
    "Где поесть в Медвежьегорске: короткая подборка ресторанов, кухня и причины зайти. Карелия"
)


@pytest.mark.parametrize(
    "intro,expected",
    [
        (MEDVEZHEGORSK_INTRO, MEDVEZHEGORSK_INTRO),
        ('  У озера\n\tподают  "пельмени" & кофе.  ', 'У озера подают "пельмени" & кофе.'),
        (" \n\t ", SEO_DESCRIPTION),
        ("", SEO_DESCRIPTION),
    ],
)
def test_city_preview_uses_intro_without_changing_search_or_home_text(database, intro, expected):
    settings, engine = database
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE locations SET intro=:intro WHERE slug='medvezhegorsk'"),
            {"intro": intro},
        )
    with TestClient(create_app(settings)) as client:
        response = client.get("/guide/medvezhegorsk")
        assert response.status_code == 200
        metadata = Metadata(response.text).values
        assert metadata["og:title"] == "Где поесть в Медвежьегорске — Инвольта гид"
        assert metadata["og:description"] == expected
        assert metadata["description"] == SEO_DESCRIPTION
        home = Metadata(client.get("/guide/").text).values
        assert home["og:title"] == "Инвольта гид"
        assert home["og:description"] == "Города. Места, где вкусно."


def test_long_intro_is_shortened_for_preview_and_preserved_on_page(database):
    settings, engine = database
    intro = "У озера пахнет соснами. " + "Горячие пельмени ждут после прогулки. " * 12
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE locations SET intro=:intro WHERE slug='medvezhegorsk'"),
            {"intro": intro},
        )
    with TestClient(create_app(settings)) as client:
        response = client.get("/guide/medvezhegorsk")
    preview = Metadata(response.text).values["og:description"]
    assert len(preview) <= 200 and preview.endswith("…")
    assert intro.startswith(preview[:-1] + " ")
    assert intro in response.text


def test_saving_intro_immediately_changes_preview_without_a_separate_copy(database):
    settings, engine = database
    with TestClient(create_app(settings)) as client:
        csrf = login(client)
        for intro in (MEDVEZHEGORSK_INTRO, 'После прогулки — кафе "Берег" & горячий кофе.'):
            with engine.connect() as conn:
                revision = conn.execute(
                    text("SELECT revision FROM locations WHERE slug='medvezhegorsk'")
                ).scalar_one()
            saved = client.post(
                "/guide/admin/locations/medvezhegorsk/intro",
                data={"_csrf": csrf, "intro": intro, "expected": revision or ""},
                headers={"origin": "http://testserver"},
            )
            assert saved.status_code == 200
            response = client.get("/guide/medvezhegorsk?preview=check")
            metadata = Metadata(response.text).values
            assert metadata["og:description"] == intro
            assert metadata["description"] == SEO_DESCRIPTION
            assert metadata["og:url"] == "http://testserver/guide/medvezhegorsk"
