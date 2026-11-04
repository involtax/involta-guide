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
    epochs = []
    api_requests = []
    epoch = [1]

    def handle(route):
        request = route.request
        path = urlsplit(request.url).path
        if path == "/":
            route.fulfill(content_type="text/html", body=html)
        elif path == "/widget.v1.js":
            route.fulfill(content_type="application/javascript", body=widget)
        elif path.endswith("/identity"):
            route.fulfill(
                content_type="application/json",
                body=json.dumps({"ready": True, "content_epoch": epoch[0]}),
            )
        elif path.endswith("/mine"):
            route.fulfill(content_type="application/json", body='{"rows":[]}')
        elif path.endswith("/submissions"):
            api_requests.append(request.method)
            assert request.headers["content-type"].startswith("audio/")
            fields = parse_qs(urlsplit(request.url).query)
            keys.append(fields["idempotency_key"][0])
            epochs.append(int(fields["content_epoch"][0]))
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
        expect(first.get_by_role("button", name="Записать отзыв", exact=True)).to_be_visible()
        expect(second.get_by_role("button", name="Записать отзыв", exact=True)).to_be_visible()
        page.evaluate("window.InvoltaVoice.mount('a')")
        expect(first.locator("section")).to_have_count(1)
        first.get_by_role("button", name="Записать отзыв", exact=True).click()
        expect(first.get_by_role("alert")).to_contain_text("Нет доступа")
        first.get_by_role("button", name="Записать отзыв", exact=True).click()
        expect(first.get_by_role("button", name="Остановить запись")).to_be_visible()
        expect(second.get_by_role("button", name="Записать отзыв", exact=True)).to_be_disabled()
        expect(first.locator(".stage .timer")).to_have_text("00:05", timeout=15000)
        first.get_by_role("button", name="Остановить запись").click()
        expect(first.get_by_role("alert")).not_to_be_empty()
        expect(first.locator("audio")).to_have_count(0)
        expect(first.get_by_role("alert")).to_contain_text("HTTP 502")
        expect(first.locator("pre").last).to_contain_text("full error response end")
        expect(first.get_by_role("link", name="Скачать аудио")).to_be_visible()
        context.route(
            "**/logs/ivanovo/uploads/*",
            lambda route: route.fulfill(
                content_type="text/html; charset=utf-8", body="<h1>Журнал отправки</h1>"
            ),
        )
        report = json.loads(first.locator("pre").first.inner_text())
        with context.expect_page() as journal:
            first.get_by_role("link", name="Открыть журнал отправки ↗").click()
        expect(journal.value).to_have_url(origin + "/logs/ivanovo/uploads/" + report["request_id"])
        expect(journal.value.get_by_role("heading", name="Журнал отправки")).to_be_visible()
        journal.value.close()
        assert page.url == origin + "/" and len(keys) == 1
        page.reload()
        expect(first.get_by_role("status")).to_contain_text("Восстановлена неотправленная запись")
        expect(first.locator("audio")).to_have_count(0)
        assert len(keys) == 1
        first.get_by_role("button", name="Повторить отправку").click()
        expect(first.get_by_role("alert")).to_contain_text("Связь прервалась")
        first.get_by_role("button", name="Повторить отправку").click()
        expect(first.get_by_role("status")).to_contain_text("Текст отзыва сохранён", timeout=10000)
        expect(first.get_by_text("Был в Огоньке сегодня, было вкусно", exact=True)).to_be_visible()
        assert keys[0] == keys[1] == keys[2] and api_requests[:3] == ["POST"] * 3
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        # A stored old-cycle intent must be deleted, never silently stamped with epoch 2.
        page.evaluate("""async () => {
          const db=await new Promise((resolve,reject)=>{const r=indexedDB.open('involta-voice-drafts',1);r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error)});
          await new Promise((resolve,reject)=>{const tx=db.transaction('drafts','readwrite');
            tx.objectStore('drafts').put({key:'voice:visitor:ivanovo',id:'old-cycle-intent',location:'ivanovo',content_epoch:1,blob:new Blob(['old private recording'],{type:'audio/webm'}),seconds:2});
            tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error)});db.close();
        }""")
        before_reset_calls = len(keys)
        epoch[0] = 2
        page.reload()
        expect(first.get_by_role("alert")).to_contain_text("Локация сброшена")
        assert len(keys) == before_reset_calls
        expect(first.get_by_role("button", name="Отправить отзыв")).to_have_count(0)
        assert (
            page.evaluate(
                """async () => {const db=await new Promise(r=>{const q=indexedDB.open('involta-voice-drafts',1);q.onsuccess=()=>r(q.result)});const count=await new Promise(r=>{const q=db.transaction('drafts').objectStore('drafts').count();q.onsuccess=()=>r(q.result)});db.close();return count}"""
            )
            == 0
        )
        first.get_by_role("button", name="Записать отзыв", exact=True).click()
        expect(first.get_by_role("alert")).to_contain_text("Нет доступа")
        first.get_by_role("button", name="Записать отзыв", exact=True).click()
        expect(first.get_by_role("button", name="Остановить запись")).to_be_visible()
        expect(first.locator(".stage .timer")).to_have_text("00:05", timeout=15000)
        first.get_by_role("button", name="Остановить запись").click()
        expect(first.get_by_role("status")).to_contain_text("Текст отзыва сохранён", timeout=10000)
        assert epochs == [1, 1, 1, 2] and keys[-1] not in {keys[0], "old-cycle-intent"}
        expect(page.locator("audio")).to_have_count(0)
        expect(page.get_by_role("button", name="Отправить отзыв", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="Записать заново", exact=True)).to_have_count(0)
        page.evaluate("window.InvoltaVoice.unmount('a');window.InvoltaVoice.unmount('b')")
        expect(first.locator("section")).to_have_count(0)
        assert page.evaluate(
            "window.__urls.size===0 && window.__streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))"
        )
        assert not errors
        browser.close()
