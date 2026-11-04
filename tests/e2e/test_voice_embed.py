import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


def test_native_recording_sends_once_and_transcription_stays_in_reviews():
    widget = (Path(__file__).parents[2] / "src/involta_voice/static/widget.v1.js").read_text()
    origin = "http://localhost:9876"
    html = """<!doctype html><meta charset="utf-8"><div id="a"></div><div id="b"></div>
      <script defer src="/widget.v1.js" data-target="a" data-api-base="/voice/api/v1" data-location="ivanovo"></script>
      <script defer src="/widget.v1.js" data-target="b" data-api-base="/voice/api/v1" data-location="ivanovo"></script>"""
    keys, rows, reports = [], [], []
    reads = 0

    def handle(route):
        nonlocal reads
        request = route.request
        path = urlsplit(request.url).path
        if path == "/":
            route.fulfill(content_type="text/html", body=html)
        elif path == "/widget.v1.js":
            route.fulfill(content_type="application/javascript", body=widget)
        elif path.endswith("/identity"):
            route.fulfill(json={"ready": True, "content_epoch": 1})
        elif path.endswith("/reviews"):
            if rows:
                reads += 1
                if reads >= 2:
                    rows[0].update(state="result_ready", transcript="Тестовый текст отзыва")
            route.fulfill(json={"rows": rows})
        elif path.endswith("/submissions"):
            assert request.headers["content-type"].startswith("audio/")
            assert len(request.post_data_buffer) > 100
            keys.append(parse_qs(urlsplit(request.url).query)["idempotency_key"][0])
            if len(keys) == 1:
                route.fulfill(
                    status=504,
                    content_type="text/html",
                    body="<html>full nginx timeout response</html>",
                )
            else:
                rows[:] = [
                    {
                        "reference": "test-submission",
                        "state": "accepted",
                        "created_at": "2026-11-04T12:00:00Z",
                        "transcript": None,
                    }
                ]
                route.fulfill(status=202, json=rows[0])
        elif path.startswith("/logs/"):
            route.fulfill(content_type="text/html; charset=utf-8", body="<h1>Журнал отправки</h1>")
        elif "/debug/" in path:
            if request.method == "POST":
                reports.append(request.post_data_json)
            route.fulfill(json={"events": [], "saved": True})
        else:
            route.fulfill(status=404)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH")
            or ("/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None),
            args=[
                "--no-sandbox",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
            ],
        )
        context = browser.new_context(
            permissions=["microphone"], viewport={"width": 390, "height": 844}
        )
        context.route("**/*", handle)
        context.add_init_script("""window.__streams=[];
          const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);let denied=false;
          navigator.mediaDevices.getUserMedia=async options=>{if(!denied){denied=true;throw new DOMException('denied','NotAllowedError')}
            const stream=await original(options);window.__streams.push(stream);return stream};""")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin)
        first, second = page.locator("#a"), page.locator("#b")
        mic = first.get_by_role("button", name="Записать отзыв", exact=True)
        expect(mic).to_be_visible()
        mic.click()
        expect(first.get_by_role("alert")).to_contain_text("Нет доступа")
        mic.click()
        expect(second.get_by_role("button", name="Записать отзыв")).to_be_disabled()
        expect(first.locator(".stage .timer")).to_have_text("00:05", timeout=15000)
        first.get_by_role("button", name="Остановить запись").click()
        expect(first.get_by_role("alert")).to_contain_text("HTTP 504")
        assert "full nginx timeout response" in reports[-1]["events"][-1]["response_body"]
        expect(first.locator("details, pre")).to_have_count(0)
        expect(first.get_by_role("link")).to_have_count(0)
        page.reload()
        expect(mic).to_be_visible()
        expect(first.locator(".status")).to_be_empty()
        expect(first.get_by_role("button", name="Повторить отправку")).to_have_count(0)
        assert len(keys) == 1
        # This is a fresh recording; the old audio has no restore or send path.
        mic.click()
        expect(first.get_by_role("alert")).to_contain_text("Нет доступа")
        mic.click()
        expect(first.locator(".stage .timer")).to_have_text("00:05", timeout=15000)
        first.get_by_role("button", name="Остановить запись").click()
        expect(first.get_by_role("heading", name="Отзывы", exact=True)).to_be_visible()
        expect(first.locator(".reviews .transcript")).to_have_text("Расшифровка…")
        expect(first.locator(".reviews .transcript")).to_have_text(
            "Тестовый текст отзыва", timeout=15000
        )
        expect(first.locator(".reviews p")).to_have_count(1)
        expect(first.locator(".status")).to_be_empty()
        expect(first.locator(".current")).to_have_count(0)
        expect(first.get_by_role("link", name="Скачать аудио")).to_have_count(0)
        assert len(keys) == 2 and keys[0] != keys[1]
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        assert page.evaluate(
            "async()=>!(await indexedDB.databases()).some(d=>d.name==='involta-voice-drafts')"
        )
        page.evaluate("InvoltaVoice.unmount('a');InvoltaVoice.unmount('b')")
        assert page.evaluate("__streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
        assert not errors
        browser.close()
