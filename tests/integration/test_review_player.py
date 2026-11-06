"""Actual player/HTMX in a browser, with local media and no database or ASR calls."""

import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import expect, sync_playwright

from involta_voice.playback import playback_response, seekable_webm

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src/involta_guide/web/static"


@pytest.mark.parametrize("width", [390, 768, 1440])
def test_review_player_progress_layout_and_htmx_preservation(streaming_webm, width):
    media = seekable_webm(streaming_webm)
    env = Environment(
        loader=FileSystemLoader(ROOT / "src/involta_guide/web/templates"), autoescape=True
    )
    player = env.get_template("review_player.html").render(
        review=SimpleNamespace(number=61, log_path="/logs/reviews/vyborg/61"),
        settings=SimpleNamespace(path=lambda p: "/guide" + p),
    )
    card = (
        '<article class="review-card" id="review-61"><header class="review-top">'
        '<div class="review-label"><h3>Отзыв № 61</h3><time>06.11.2026 20:58 UTC</time></div>'
        + player
        + '<div class="review-actions"><form><button class="review-delete">Перераспознать</button></form>'
        '<form><button class="review-delete">Удалить</button></form></div></header>'
        '<dl class="review-transcripts"><div><dt>GigaAM</dt><dd>Тестовый текст отзыва.</dd></div></dl></article>'
    )
    html = (
        '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/admin.css">'
        '<script defer src="/htmx.min.js"></script><script defer src="/review-player.js"></script>'
        '<main class="admin-workspace"><h2>Выборг</h2>' + card + "</main></html>"
    )
    folder = Path(
        os.getenv("REVIEW_PLAYER_SCREENSHOTS_DIR", str(ROOT / "docs/design/admin-review-player"))
    )
    folder.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        executable = os.getenv("CHROMIUM_PATH")
        browser = pw.chromium.launch(**({"executable_path": executable} if executable else {}))
        page = browser.new_page(viewport={"width": width, "height": 600})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def respond(route):
            path = urlsplit(route.request.url).path
            if path.endswith("/audio"):
                response = playback_response(media, route.request.headers.get("range", ""))
                route.fulfill(
                    status=response.status_code, headers=dict(response.headers), body=response.body
                )
            elif path == "/fragment":
                route.fulfill(body=card, content_type="text/html")
            elif path == "/remove":
                route.fulfill(body="<p>Карточка удалена</p>", content_type="text/html")
            elif path in ["/style.css", "/admin.css", "/htmx.min.js", "/review-player.js"]:
                route.fulfill(
                    path=str(STATIC / path[1:]),
                    content_type="text/css" if path.endswith(".css") else "application/javascript",
                )
            elif path == "/missing":
                route.fulfill(status=404, body="missing")
            else:
                route.fulfill(body=html, content_type="text/html")

        page.route("**/*", respond)
        page.goto("http://player.test/")
        audio = page.locator("audio")
        controls = page.locator(".review-player-controls")
        button = page.locator(".review-play")
        seek = page.locator(".review-seek")
        expect(controls).to_be_visible()
        page.wait_for_function('Number.isFinite(document.querySelector("audio").duration)')
        assert audio.evaluate("a=>a.currentTime") == 0
        assert audio.evaluate("a=>a.controls") is False
        assert seek.input_value() == "0"
        assert seek.evaluate('e=>e.style.getPropertyValue("--progress")') == "0%"
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        actions = page.locator(".review-actions button").evaluate_all(
            "bs=>bs.map(b=>b.getBoundingClientRect().y)"
        )
        assert abs(actions[0] - actions[1]) < 1
        if width == 1440:
            assert (
                abs(
                    controls.bounding_box()["y"]
                    - page.locator(".review-actions").bounding_box()["y"]
                )
                < 10
            )
        page.locator(".admin-workspace").screenshot(path=str(folder / f"initial-{width}.png"))
        before = controls.bounding_box()
        audio.evaluate(
            "a=>{window.realPlay=a.play.bind(a);a.play=()=>new Promise((resolve,reject)=>{window.rejectPlay=reject})}"
        )
        button.click()
        expect(button).to_be_disabled()
        assert button.evaluate("b=>getComputedStyle(b).opacity") == "1"
        expect(page.locator("[data-play-icon]")).to_be_visible()
        page.evaluate('window.rejectPlay(new DOMException("blocked","NotAllowedError"))')
        expect(page.locator(".review-player-error")).to_be_visible()
        expect(button).to_be_enabled()
        assert audio.evaluate("a=>a.paused")
        audio.evaluate("a=>{a.play=window.realPlay}")
        button.click()
        page.wait_for_function('document.querySelector("audio").currentTime>0.3')
        expect(page.locator("[data-pause-icon]")).to_be_visible()
        assert controls.bounding_box()["height"] == before["height"]
        assert controls.bounding_box()["width"] == before["width"]
        button.click()
        assert audio.evaluate("a=>a.paused")
        seek.click(position={"x": seek.bounding_box()["width"] / 2, "y": 2})
        page.wait_for_function(
            'Math.abs(document.querySelector("audio").currentTime-document.querySelector("audio").duration/2)<0.6'
        )
        seek.press("Home")
        page.wait_for_function('document.querySelector("audio").currentTime===0')
        assert seek.input_value() == "0"
        button.click()
        page.wait_for_function('document.querySelector("audio").currentTime>0.2')
        page.evaluate('window.originalAudio=document.querySelector("audio")')
        old_time = audio.evaluate("a=>a.currentTime")
        for _ in range(3):
            page.evaluate('htmx.ajax("GET","/fragment",{target:"#review-61",swap:"outerHTML"})')
        assert audio.evaluate("a=>a===window.originalAudio")
        assert not audio.evaluate("a=>a.paused")
        assert audio.evaluate("a=>a.currentTime") >= old_time
        expect(controls).to_have_count(1)
        page.locator(".admin-workspace").screenshot(path=str(folder / f"playing-{width}.png"))
        audio.evaluate("a=>{a.currentTime=a.duration-0.15}")
        page.wait_for_function('document.querySelector("audio").ended')
        expect(page.locator("[data-play-icon]")).to_be_visible()
        audio.evaluate(
            "a=>{window.playCalls=0;const play=a.play.bind(a);a.play=()=>{window.playCalls++;return play()}}"
        )
        button.click()
        page.wait_for_function(
            '!document.querySelector("audio").paused&&document.querySelector("audio").currentTime<2'
        )
        assert page.evaluate("window.playCalls") == 1
        page.evaluate('htmx.ajax("GET","/remove",{target:"#review-61",swap:"outerHTML"})')
        page.wait_for_function("window.originalAudio.paused")
        assert page.locator("audio").count() == 0
        assert not errors
        page.goto("http://player.test/")
        page.wait_for_function('document.querySelector("audio").controls===false')
        page.locator("audio").evaluate('a=>{a.src="/missing";a.load()}')
        expect(page.locator(".review-player-error")).to_contain_text("Не удалось загрузить аудио.")
        expect(page.locator(".review-play")).to_be_disabled()
        # Progressive enhancement keeps native playback when scripts cannot run.
        fallback = browser.new_page(java_script_enabled=False)
        fallback.route("**/*", respond)
        fallback.goto("http://player.test/")
        assert fallback.locator("audio").get_attribute("controls") is not None
        expect(fallback.locator(".review-player-controls")).to_be_hidden()
        browser.close()
