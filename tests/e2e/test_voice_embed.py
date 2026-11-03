import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


def test_widget_permission_html_error_reload_and_lost_202():
    widget = (Path(__file__).parents[2] / "src/involta_voice/static/widget.v1.js").read_text()
    origin = "http://localhost:9876"
    html = """<!doctype html><meta charset="utf-8"><div id="a"></div><div id="b"></div>
      <script defer src="/widget.v1.js" data-target="a" data-api-base="/voice/api/v1" data-location="ivanovo"></script>
      <script defer src="/widget.v1.js" data-target="b" data-api-base="/voice/api/v1" data-location="ivanovo"></script>"""
    keys = []
    api_requests = []

    def handle(route):
        request = route.request
        path = urlsplit(request.url).path
        if path == "/":
            route.fulfill(content_type="text/html", body=html)
        elif path == "/widget.v1.js":
            route.fulfill(content_type="application/javascript", body=widget)
        elif path.endswith("/identity"):
            route.fulfill(content_type="application/json", body='{"ready":true,"content_epoch":1}')
        elif path.endswith("/mine"):
            route.fulfill(content_type="application/json", body='{"rows":[]}')
        elif path.endswith("/submissions"):
            api_requests.append(request.method)
            assert request.headers["content-type"].startswith("audio/")
            fields = parse_qs(urlsplit(request.url).query)
            keys.append(fields["idempotency_key"][0])
            assert request.post_data_buffer
            if len(keys) == 1:
                route.fulfill(
                    status=502,
                    content_type="text/html",
                    body="<html><h1>Proxy upload failed</h1><p>full error response end</p></html>",
                )
            elif len(keys) == 2:
                route.abort("failed")
            else:
                route.fulfill(
                    status=202,
                    content_type="application/json",
                    body=json.dumps({"reference": "test-submission", "state": "accepted"}),
                )
        elif path.endswith("/test-submission"):
            api_requests.append(request.method)
            route.fulfill(
                content_type="application/json",
                body=json.dumps(
                    {
                        "state": "analysis_failed",
                        "moderation": "pending",
                        "transcript": "Был в Огоньке сегодня, было вкусно",
                    }
                ),
            )
        else:
            route.fulfill(status=404)

    with sync_playwright() as p:
        import os

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
        context.add_init_script("""window.__streams=[];window.__urls=new Set();
          const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);let denied=false;
          navigator.mediaDevices.getUserMedia=async options=>{if(!denied){denied=true;throw new DOMException('denied','NotAllowedError')}
            const stream=await original(options);window.__streams.push(stream);return stream};
          const create=URL.createObjectURL.bind(URL),revoke=URL.revokeObjectURL.bind(URL);
          URL.createObjectURL=blob=>{const url=create(blob);window.__urls.add(url);return url};
          URL.revokeObjectURL=url=>{window.__urls.delete(url);revoke(url)};""")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", handle)
        page.goto(origin)
        first = page.locator("#a")
        second = page.locator("#b")
        expect(first.get_by_role("button", name="● Записать отзыв")).to_be_visible()
        expect(second.get_by_role("button", name="● Записать отзыв")).to_be_visible()
        page.evaluate("window.InvoltaVoice.mount('a')")
        expect(first.locator("section")).to_have_count(1)
        first.get_by_role("button", name="● Записать отзыв").click()
        expect(first.get_by_role("alert")).to_contain_text("Нет доступа")
        first.get_by_role("button", name="● Записать отзыв").click()
        expect(first.get_by_role("button", name="■ Остановить запись")).to_be_visible()
        expect(second.get_by_role("button", name="● Записать отзыв")).to_be_enabled()
        page.wait_for_timeout(1200)
        first.get_by_role("button", name="■ Остановить запись").click()
        expect(first.locator("audio")).to_be_visible()
        first.get_by_role("button", name="Записать заново").click()
        expect(first.get_by_role("button", name="■ Остановить запись")).to_be_visible()
        page.wait_for_timeout(1200)
        first.get_by_role("button", name="■ Остановить запись").click()
        first.get_by_role("button", name="Отправить отзыв").click()
        expect(first.get_by_role("alert")).not_to_be_empty()
        expect(first.locator("audio")).to_be_visible()
        expect(first.get_by_role("alert")).to_contain_text("HTTP 502")
        expect(first.locator("pre").last).to_contain_text("full error response end")
        expect(first.get_by_role("link", name="Скачать аудио")).to_be_visible()
        page.reload()
        expect(first.get_by_role("status")).to_contain_text("Восстановлена неотправленная запись")
        expect(first.locator("audio")).to_be_visible()
        first.get_by_role("button", name="Отправить отзыв").click()
        expect(first.get_by_role("alert")).to_contain_text("Связь прервалась")
        first.get_by_role("button", name="Отправить отзыв").click()
        expect(first.get_by_role("status")).to_contain_text("Текст отзыва сохранён", timeout=10000)
        expect(first.get_by_text("Был в Огоньке сегодня, было вкусно", exact=True)).to_be_visible()
        assert keys[0] == keys[1] == keys[2] and api_requests[:3] == ["POST"] * 3
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        page.evaluate("window.InvoltaVoice.unmount('a');window.InvoltaVoice.unmount('b')")
        expect(first.locator("section")).to_have_count(0)
        assert page.evaluate(
            "window.__urls.size===0 && window.__streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))"
        )
        assert not errors
        browser.close()
