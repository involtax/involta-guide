import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

WIDGET = (Path(__file__).parents[2] / "src/involta_voice/static/widget.v1.js").read_text()
REQUEST = "твой голос важен. скажи что ты думаешь и о каком ресторане"
FAKE_RECORDER = r"""
window.__streams=[];window.__contexts=[];window.__recorders=[];
const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getUserMedia=async options=>{const stream=await original(options);window.__streams.push(stream);
 if(window.__delayPermission){return await new Promise(resolve=>window.__latePermission=()=>resolve(stream))}return stream};
const Audio=window.AudioContext;window.AudioContext=class extends Audio{constructor(...a){super(...a);window.__contexts.push(this)}};
window.MediaRecorder=class {
 static isTypeSupported(){return true} constructor(stream, options){this.stream=stream;this.mimeType=options?.mimeType||'audio/webm';this.state='inactive';window.__recorders.push(this)}
 start(){this.state='recording';queueMicrotask(()=>this.onstart?.())}
 stop(){if(this.state==='inactive')return;this.state='inactive';this.stops=(this.stops||0)+1;setTimeout(()=>{this.ondataavailable?.({data:new Blob(['test audio'.repeat(30)],{type:this.mimeType})});this.onstop?.()},window.__encodingDelay||0)}
};
"""
FAKE_VAD = """window.vad={MicVAD:{new:async options=>{window.__vadOptions=options;return {model:{release:async()=>{}},start:async()=>{},destroy:async()=>{}}}}};"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or "/usr/bin/chromium",
            args=[
                "--no-sandbox",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        yield browser
        browser.close()


@pytest.fixture
def widget_page(browser):
    context = browser.new_context(
        permissions=["microphone"], viewport={"width": 390, "height": 844}, has_touch=True
    )
    context.add_init_script(FAKE_RECORDER)
    page = context.new_page()
    uploads = []
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def respond(route):
        path = urlsplit(route.request.url).path
        if path == "/":
            route.fulfill(
                content_type="text/html",
                body='<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><div id="a"></div><div id="b"></div><script src="/guide/voice/widget.v1.js" data-target="a" data-location="ivanovo"></script><script src="/guide/voice/widget.v1.js" data-target="b" data-location="shuya"></script>',
            )
        elif path.endswith("widget.v1.js"):
            route.fulfill(content_type="application/javascript", body=WIDGET)
        elif path.endswith("ort.wasm.min.js"):
            route.fulfill(content_type="application/javascript", body="window.ort={env:{wasm:{}}}")
        elif path.endswith("bundle.min.js"):
            route.fulfill(content_type="application/javascript", body=FAKE_VAD)
        elif path.endswith("/submissions"):
            uploads.append(route.request)
            route.fulfill(
                status=202,
                content_type="application/json",
                body='{"reference":"saved","state":"accepted"}',
            )
        elif path.endswith("/identity"):
            route.fulfill(content_type="application/json", body='{"ready":true,"content_epoch":1}')
        else:
            route.fulfill(
                content_type="application/json",
                body='{"rows":[],"state":"result_ready","transcript":"Отзыв сохранён","events":[],"saved":true}',
            )

    page.route("**/*", respond)
    page.goto("http://localhost:9876")
    page.clock.install()
    page.clock.pause_at(datetime.now(UTC))
    yield page, page.locator("#a"), uploads
    expect(page.locator("audio")).to_have_count(0)
    expect(page.get_by_role("button", name="Отправить отзыв", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Записать заново", exact=True)).to_have_count(0)
    assert not errors
    context.close()


def wait_expression(page, expression):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if page.evaluate(expression):
            return
        time.sleep(0.01)
    raise AssertionError(expression)


def press(page, widget):
    widget.locator(".primary").dispatch_event(
        "pointerdown", {"isPrimary": True, "button": 0, "pointerType": "touch"}
    )
    widget.locator(".primary").dispatch_event("pointerup")


def state(widget, value):
    expect(widget).to_have_attribute("data-voice-state", value)


def report(widget):
    return json.loads(widget.locator("details pre").first.text_content())


def touch(cdp, kind, x=0, y=0):
    cdp.send(
        "Input.dispatchTouchEvent",
        {"type": kind, "touchPoints": [] if kind == "touchEnd" else [{"x": x, "y": y, "id": 1}]},
    )


@pytest.mark.parametrize("hold_ms", [0, 3000, 7000])
def test_native_upward_drag_after_holding_continues_one_recording(widget_page, hold_ms):
    page, widget, uploads = widget_page
    primary = widget.locator(".primary")
    box = primary.bounding_box()
    x, y = box["x"] + 40, box["y"] + 40
    cdp = page.context.new_cdp_session(page)
    primary.evaluate(
        "e=>e.addEventListener('pointerdown',event=>window.__dragPointer=event.pointerId)"
    )
    if hold_ms == 7000:
        page.emulate_media(reduced_motion="reduce")
    touch(cdp, "touchStart", x, y)
    state(widget, "recording")
    page.clock.run_for(hold_ms)
    touch(cdp, "touchMove", x + 20, y - 140)
    offset = primary.evaluate("e=>parseFloat(e.style.getPropertyValue('--drag-y'))")
    assert -80 < offset < -60
    assert primary.evaluate("e=>e.hasPointerCapture(window.__dragPointer)")
    assert (
        primary.bounding_box()["y"]
        >= widget.locator("h2").bounding_box()["y"] + widget.locator("h2").bounding_box()["height"]
    )
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    if hold_ms == 7000:
        assert primary.evaluate("e=>getComputedStyle(e).transitionDuration") == "0s"
    assert primary.evaluate("e=>getComputedStyle(e).webkitTapHighlightColor") == "rgba(0, 0, 0, 0)"
    assert primary.evaluate("e=>getComputedStyle(e).touchAction") == "none"
    touch(cdp, "touchEnd")
    state(widget, "recording")
    assert primary.evaluate(
        "e=>!e.classList.contains('dragging') && !e.style.getPropertyValue('--drag-y')"
    )
    assert page.evaluate("__recorders.length===1 && !__recorders[0].stops")
    assert not uploads
    gesture = next(e for e in report(widget)["timeline"] if e["name"] == "gesture_end")
    assert gesture["reason"] == "released" and gesture["lift_px"] == 140
    expect(widget.get_by_text(REQUEST, exact=True)).to_be_visible()
    page.keyboard.press("Escape")


def test_native_drag_owns_its_touch_but_outside_scroll_does_not_stop_recording(widget_page):
    page, widget, uploads = widget_page
    page.add_style_tag(content="body{margin:0;min-height:4000px}")
    events = []
    page.expose_function("capturePointerEvent", lambda event: events.append(event))
    primary = widget.locator(".primary")
    primary.evaluate(
        "e=>{for(const t of ['pointerdown','pointercancel','pointerup'])e.addEventListener(t,event=>capturePointerEvent(event.type))}"
    )
    box = primary.bounding_box()
    x, y = box["x"] + 40, box["y"] + 40
    cdp = page.context.new_cdp_session(page)
    touch(cdp, "touchStart", x, y)
    state(widget, "recording")
    for delta in [20, 60, 100, 150]:
        touch(cdp, "touchMove", x, y - delta)
    touch(cdp, "touchEnd")
    assert page.evaluate("scrollY") == 0
    assert events == ["pointerdown", "pointerup"]
    touch(cdp, "touchStart", 12, 700)
    for delta in [20, 60, 100, 200]:
        touch(cdp, "touchMove", 12, 700 - delta)
    touch(cdp, "touchEnd")
    assert page.evaluate("scrollY") > 0
    state(widget, "recording")
    assert not uploads
    page.keyboard.press("Escape")


def test_late_permission_drag_release_never_cancels_or_restarts_capture(widget_page):
    page, widget, uploads = widget_page
    page.evaluate("window.__delayPermission=true")
    primary = widget.locator(".primary")
    box = primary.bounding_box()
    x, y = box["x"] + 40, box["y"] + 40
    cdp = page.context.new_cdp_session(page)
    touch(cdp, "touchStart", x, y)
    state(widget, "starting")
    wait_expression(page, "!!window.__latePermission")
    page.clock.run_for(7000)
    touch(cdp, "touchMove", x, y - 140)
    touch(cdp, "touchEnd")
    state(widget, "starting")
    page.evaluate("__latePermission();window.__delayPermission=false")
    state(widget, "recording")
    assert page.evaluate("__recorders.length") == 1
    assert not uploads
    page.keyboard.press("Escape")


def test_lost_capture_resets_drag_and_finalizes_valid_audio_once(widget_page):
    page, widget, uploads = widget_page
    primary = widget.locator(".primary")
    primary.dispatch_event("pointerdown", {"isPrimary": True, "button": 0, "pointerId": 4})
    state(widget, "recording")
    page.clock.run_for(5100)
    primary.dispatch_event("pointermove", {"pointerId": 4, "clientY": -120})
    primary.dispatch_event("lostpointercapture", {"pointerId": 4})
    primary.dispatch_event("pointercancel", {"pointerId": 4})
    page.clock.run_for(1)
    state(widget, "retry")
    assert page.evaluate("__recorders[0].stops") == 1
    assert not primary.evaluate("e=>e.classList.contains('dragging')")
    assert not uploads


@pytest.mark.parametrize("ending", ["escape", "limit", "unmount"])
def test_ending_while_finger_is_held_releases_capture_without_restarting(widget_page, ending):
    page, widget, uploads = widget_page
    primary = widget.locator(".primary")
    box = primary.bounding_box()
    cdp = page.context.new_cdp_session(page)
    primary.evaluate(
        "e=>{window.__heldButton=e;e.addEventListener('pointerdown',event=>window.__heldPointer=event.pointerId)}"
    )
    touch(cdp, "touchStart", box["x"] + 40, box["y"] + 40)
    state(widget, "recording")
    page.clock.run_for(5100)
    if ending == "escape":
        page.keyboard.press("Escape")
    elif ending == "limit":
        page.clock.fast_forward(120000)
    else:
        page.evaluate("InvoltaVoice.unmount('a')")
    page.clock.run_for(1)
    touch(cdp, "touchEnd")
    assert page.evaluate(
        "!__heldButton.hasPointerCapture(__heldPointer) && !__heldButton.classList.contains('dragging')"
    )
    assert page.evaluate("__recorders.length===1 && __recorders[0].stops===1")
    assert page.evaluate("__streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
    if ending == "limit":
        state(widget, "accepted")
    assert len(uploads) == (1 if ending == "limit" else 0)


def test_gate_exact_pause_reset_unknown_frames_and_no_speech(widget_page):
    page, _, _ = widget_page
    result = page.evaluate("""() => {
      const Gate=InvoltaVoice.SilenceGate; const gate=new Gate();let t=0,result;
      const feed=(p,n)=>{for(let i=0;i<n;i++)result=gate.frame(p,++t,1);return result};
      feed(.9,400);const before=feed(.1,4499),warning=feed(.1,1),almost=feed(.1,499);
      const reset=feed(.9,1);feed(.1,4999);const end=feed(.1,1);
      const unknown=new Gate();for(let i=1;i<=400;i++)unknown.frame(.9,i,1);for(let i=401;i<=5399;i++)unknown.frame(.1,i,1);
      const uncertain=unknown.frame(.275,5400,1);const afterUncertain=unknown.frame(.1,5401,1);
      const absent=new Gate();let noSpeech;for(let i=1;i<=8000;i++)noSpeech=absent.frame(.1,i,1);
      const lag=new Gate();lag.frame(.9,0);const stalled=lag.frame(.1,1000);
      return {before,warning,almost,reset,end,uncertain,afterUncertain,noSpeech,stalled};
    }""")
    assert not result["before"]["warning"] and result["warning"]["warning"]
    assert result["almost"]["stop"] is None and not result["reset"]["warning"]
    assert result["end"]["stop"] == "silence"
    assert result["uncertain"]["stop"] is None and not result["afterUncertain"]["warning"]
    assert result["noSpeech"]["stop"] == "no_speech" and result["stalled"]["unavailable"]


@pytest.mark.parametrize("duration", [4999, 5000, 5001])
def test_minimum_uses_stop_time_and_only_valid_audio_is_sent_automatically(widget_page, duration):
    page, widget, uploads = widget_page
    page.evaluate("window.__encodingDelay=250")
    original = widget.locator(".primary").element_handle()
    press(page, widget)
    state(widget, "recording")
    expect(widget.get_by_text(REQUEST, exact=True)).to_be_visible()
    # Initial pointer release and its click cannot hit the stop on the same DOM node.
    widget.locator(".primary").dispatch_event("pointerup", {"isPrimary": True, "button": 0})
    widget.locator(".primary").dispatch_event("click", {"detail": 1})
    state(widget, "recording")
    assert original.evaluate("el=>el===el.getRootNode().querySelector('.primary')")
    page.clock.run_for(duration)
    press(page, widget)
    state(widget, "stopping")
    page.clock.run_for(250)
    state(widget, "idle" if duration < 5000 else "accepted")
    assert len(uploads) == (0 if duration < 5000 else 1)
    expect(widget.get_by_text(REQUEST, exact=True)).to_be_visible()
    events = report(widget)["timeline"]
    assert next(e["duration_ms"] for e in events if e["name"] == "stop_requested") == duration
    if duration < 5000:
        expect(widget.get_by_role("status")).to_contain_text("Слишком коротко")
        assert "draft" not in report(widget)
        assert page.evaluate("__streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
    else:
        assert next(e["duration_ms"] for e in events if e["name"] == "blob_ready") == duration
        assert report(widget)["draft"] is None


def test_cancel_permission_late_stream_and_second_embed_are_safe(widget_page):
    page, widget, uploads = widget_page
    page.evaluate("window.__delayPermission=true")
    press(page, widget)
    state(widget, "starting")
    expect(page.locator("#b").get_by_role("button", name="Записать отзыв")).to_be_disabled()
    wait_expression(page, "!!window.__latePermission")
    widget.get_by_role("button", name="Отменить").dispatch_event("click")
    state(widget, "idle")
    page.evaluate("window.__latePermission();window.__delayPermission=false")
    wait_expression(page, "__streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
    assert page.evaluate("__recorders.length") == 0
    press(page, widget)
    state(widget, "recording")
    page.keyboard.press("Escape")
    state(widget, "idle")
    page.clock.run_for(1)
    assert uploads == []
    expect(widget.get_by_role("status")).to_be_empty()


def test_interruption_needs_retry_and_limit_sends_once(widget_page):
    page, widget, uploads = widget_page
    press(page, widget)
    state(widget, "recording")
    page.clock.run_for(5100)
    widget.locator(".primary").dispatch_event("pointercancel")
    page.clock.run_for(1)
    state(widget, "retry")
    assert page.evaluate("__recorders[0].stops") == 1 and not uploads
    press(page, widget)
    state(widget, "accepted")
    assert len(uploads) == 1
    press(page, widget)
    state(widget, "recording")
    page.clock.fast_forward(120000)
    page.clock.run_for(1)
    state(widget, "accepted")
    assert page.evaluate("__recorders[1].stops") == 1
    assert len(uploads) == 2
    assert (
        next(e for e in report(widget)["timeline"] if e["name"] == "stop_requested")["reason"]
        == "limit"
    )


def test_unmount_keeps_finalized_valid_draft_and_closes_resources(widget_page):
    page, widget, uploads = widget_page
    press(page, widget)
    state(widget, "recording")
    page.clock.run_for(5100)
    page.evaluate("InvoltaVoice.unmount('a')")
    page.clock.run_for(1)
    expect(widget.locator("section")).to_have_count(0)
    wait_expression(
        page,
        "__contexts.every(c=>c.state==='closed') && __streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))",
    )
    page.evaluate("InvoltaVoice.mount('a',{location:'ivanovo'})")
    state(widget, "retry")
    expect(widget.get_by_role("status")).to_contain_text("Восстановлена")
    assert not uploads


def test_keyboard_repeat_and_pointer_holds_never_toggle_twice(widget_page):
    page, widget, uploads = widget_page
    widget.locator(".primary").focus()
    page.keyboard.down("Enter")
    state(widget, "recording")
    page.keyboard.down("Enter")
    page.keyboard.up("Enter")
    state(widget, "recording")
    page.clock.run_for(3000)
    widget.locator(".primary").dispatch_event(
        "pointermove", {"isPrimary": True, "clientX": 1, "clientY": 1}
    )
    widget.locator(".primary").dispatch_event("pointerup", {"isPrimary": True, "button": 0})
    state(widget, "recording")
    page.clock.run_for(2100)
    page.keyboard.press("Space")
    page.clock.run_for(1)
    state(widget, "accepted")
    assert page.evaluate("__recorders.length===1 && __recorders[0].stops===1")
    assert len(uploads) == 1


@pytest.mark.parametrize("speech", [True, False])
def test_vad_autostop_sends_once_even_when_speech_was_not_detected(widget_page, speech):
    page, widget, uploads = widget_page
    press(page, widget)
    state(widget, "recording")
    page.clock.run_for(32)
    wait_expression(page, "!!window.__vadOptions && __contexts[0].state==='running'")
    sequence = ([0.9] * 20 + [0.1] * 158) if speech else [0.1] * 251
    for probability in sequence:
        page.clock.run_for(32)
        page.evaluate(
            "p=>window.__vadOptions.onFrameProcessed({isSpeech:p},new Float32Array(512))",
            probability,
        )
    page.clock.run_for(1)
    state(widget, "accepted")
    assert len(uploads) == 1
    assert page.evaluate("__recorders[0].stops") == 1
    reason = next(e for e in report(widget)["timeline"] if e["name"] == "stop_requested")["reason"]
    assert reason == ("silence" if speech else "no_speech")


def test_vad_failure_keeps_manual_stop_and_never_treats_missing_frames_as_silence(widget_page):
    page, widget, uploads = widget_page
    page.route("**/bundle.min.js", lambda route: route.fulfill(status=503))
    press(page, widget)
    state(widget, "recording")
    page.clock.run_for(32)
    expect(widget.get_by_text("Автостоп недоступен.", exact=False)).to_be_visible()
    page.clock.run_for(9000)
    state(widget, "recording")
    press(page, widget)
    page.clock.run_for(1)
    state(widget, "accepted")
    assert len(uploads) == 1


def test_old_short_draft_remains_downloadable_and_does_not_block_new_recording(widget_page):
    page, widget, uploads = widget_page
    page.evaluate("""async()=>{
      InvoltaVoice.unmount('a');const db=await new Promise((resolve,reject)=>{const r=indexedDB.open('involta-voice-drafts',1);r.onsuccess=()=>resolve(r.result);r.onerror=reject});
      await new Promise(resolve=>{const tx=db.transaction('drafts','readwrite');tx.objectStore('drafts').put({key:'voice:visitor:ivanovo',id:'old-short',blob:new Blob(['old'],{type:'audio/webm'}),seconds:2,content_epoch:1,location:'ivanovo'});tx.oncomplete=resolve});db.close();
      InvoltaVoice.mount('a',{location:'ivanovo'});
    }""")
    state(widget, "retry")
    expect(widget.get_by_role("button", name="Записать отзыв")).to_be_enabled()
    expect(widget.get_by_role("button", name="Повторить отправку")).to_have_count(0)
    expect(widget.get_by_role("link", name="Скачать аудио")).to_be_visible()
    assert not uploads
    primary = widget.locator(".primary")
    primary.dispatch_event("pointerdown", {"isPrimary": True, "button": 0, "pointerId": 4})
    state(widget, "recording")
    primary.dispatch_event("pointermove", {"pointerId": 4, "clientY": -100})
    assert primary.evaluate(
        "e=>e.classList.contains('dragging') && parseFloat(e.style.getPropertyValue('--drag-y'))<0"
    )
    primary.dispatch_event("pointerup", {"pointerId": 4})
    page.keyboard.press("Escape")


def test_server_rejected_duration_preserves_download_without_invalid_retry(widget_page):
    page, widget, uploads = widget_page
    rejected = []

    def reject(route):
        rejected.append(route.request)
        route.fulfill(
            status=422,
            content_type="application/json",
            body='{"error":{"code":"audio_too_short","message":"Запись короче 5 секунд","min_duration_seconds":5}}',
        )

    page.route("**/submissions?*", reject)
    press(page, widget)
    state(widget, "recording")
    page.clock.run_for(5100)
    press(page, widget)
    page.clock.run_for(1)
    state(widget, "retry")
    expect(widget.get_by_role("alert")).to_contain_text("короче 5 секунд")
    expect(widget.get_by_role("button", name="Повторить отправку")).to_have_count(0)
    expect(widget.get_by_role("button", name="Записать отзыв")).to_be_enabled()
    expect(widget.get_by_role("link", name="Скачать аудио")).to_be_visible()
    expect(widget.get_by_text(REQUEST, exact=True)).to_be_visible()
    wait_expression(
        page,
        """(async()=>{
      const db=await new Promise(r=>{const q=indexedDB.open('involta-voice-drafts',1);q.onsuccess=()=>r(q.result)});
      const saved=await new Promise(r=>{const q=db.transaction('drafts').objectStore('drafts').get('voice:visitor:ivanovo');q.onsuccess=()=>r(q.result)});
      db.close();return !!saved?.blocked;
    })()""",
    )
    page.reload()
    state(widget, "retry")
    expect(widget.get_by_role("button", name="Повторить отправку")).to_have_count(0)
    expect(widget.get_by_role("button", name="Записать отзыв")).to_be_enabled()
    expect(widget.get_by_role("link", name="Скачать аудио")).to_be_visible()
    assert len(rejected) == 1


def test_permission_wait_is_not_audio_duration(widget_page):
    page, widget, uploads = widget_page
    page.evaluate("window.__delayPermission=true")
    press(page, widget)
    state(widget, "starting")
    wait_expression(page, "!!window.__latePermission")
    page.clock.run_for(7000)
    expect(widget.locator(".timer")).to_be_empty()
    page.evaluate("window.__latePermission();window.__delayPermission=false")
    state(widget, "recording")
    page.clock.run_for(4999)
    press(page, widget)
    page.clock.run_for(1)
    state(widget, "idle")
    assert not uploads
    assert (
        next(e["duration_ms"] for e in report(widget)["timeline"] if e["name"] == "stop_requested")
        == 4999
    )


def test_browser_initiated_recorder_stop_uses_same_duration_check(widget_page):
    page, widget, uploads = widget_page
    press(page, widget)
    state(widget, "recording")
    page.clock.run_for(5100)
    page.evaluate("__recorders[0].stop()")
    page.clock.run_for(1)
    state(widget, "retry")
    assert report(widget)["draft"]["durationMs"] == 5100
    assert not uploads


@pytest.mark.parametrize("reset_at", ["identity", "upload"])
def test_reset_rejects_new_widget_draft_without_relabelling_its_epoch(widget_page, reset_at):
    page, widget, uploads = widget_page
    press(page, widget)
    state(widget, "recording")
    page.clock.run_for(5100)
    attempted_epochs = []
    if reset_at == "identity":
        page.route(
            "**/identity?*",
            lambda route: route.fulfill(
                content_type="application/json", body='{"ready":true,"content_epoch":2}'
            ),
        )
    else:
        from urllib.parse import parse_qs

        def reject_stale(route):
            attempted_epochs.append(parse_qs(urlsplit(route.request.url).query)["content_epoch"])
            route.fulfill(
                status=409,
                content_type="application/json",
                body='{"detail":{"code":"stale_epoch","message":"Локация сброшена"}}',
            )

        page.route("**/submissions?*", reject_stale)
    press(page, widget)
    page.clock.run_for(1)
    state(widget, "idle")
    expect(widget.get_by_role("alert")).to_contain_text("Локация сброшена")
    expect(widget.get_by_text(REQUEST, exact=True)).to_be_visible()
    assert not uploads
    assert attempted_epochs == ([] if reset_at == "identity" else [["1"]])
    assert (
        page.evaluate("""async()=>{
      const db=await new Promise(resolve=>{const q=indexedDB.open('involta-voice-drafts',1);q.onsuccess=()=>resolve(q.result)});
      const count=await new Promise(resolve=>{const q=db.transaction('drafts').objectStore('drafts').count();q.onsuccess=()=>resolve(q.result)});
      db.close();return count;
    }""")
        == 0
    )


def test_auto_upload_disables_control_and_coalesces_duplicate_stop_events(widget_page):
    page, widget, _ = widget_page
    pending = []
    page.route("**/submissions?*", lambda route: pending.append(route))
    press(page, widget)
    state(widget, "recording")
    page.clock.run_for(5100)
    with page.expect_request("**/submissions?*"):
        press(page, widget)
        widget.locator(".primary").dispatch_event("pointercancel")
        page.clock.run_for(1)
    state(widget, "uploading")
    expect(widget.get_by_role("button", name="Отправляем отзыв", exact=True)).to_be_disabled()
    assert report(widget)["draft"]["local_copy_saved"]
    press(page, widget)
    widget.locator(".primary").dispatch_event("click", {"detail": 0})
    assert len(pending) == 1
    pending[0].fulfill(status=202, json={"reference": "saved", "state": "accepted"})
    state(widget, "accepted")
    assert page.evaluate("__recorders.length===1 && __recorders[0].stops===1")
    expect(widget.get_by_role("button", name="Записать отзыв", exact=True)).to_be_enabled()


def test_unmount_during_upload_identity_keeps_audio_without_a_late_post(widget_page):
    page, widget, uploads = widget_page
    press(page, widget)
    state(widget, "recording")
    page.clock.run_for(5100)
    pending = []
    page.route("**/identity?*", lambda route: pending.append(route))
    page.evaluate("""()=>{const native=fetch;window.__identityResolved=false;window.fetch=async(...args)=>{
      const response=await native(...args);if(String(args[0]).includes('/identity?'))window.__identityResolved=true;return response;
    }}""")
    with page.expect_request("**/identity?*"):
        press(page, widget)
        page.clock.run_for(1)
    state(widget, "uploading")
    page.evaluate("InvoltaVoice.unmount('a')")
    pending[0].fulfill(json={"ready": True, "content_epoch": 1})
    wait_expression(page, "window.__identityResolved")
    page.unroute("**/identity?*")
    page.evaluate("InvoltaVoice.mount('a',{location:'ivanovo'})")
    state(widget, "retry")
    assert not uploads
    press(page, widget)
    state(widget, "accepted")
    assert len(uploads) == 1
    assert page.evaluate("__recorders.length") == 1


def test_late_status_of_previous_review_cannot_overwrite_a_new_recording(widget_page):
    page, widget, uploads = widget_page
    press(page, widget)
    state(widget, "recording")
    page.clock.run_for(5100)
    press(page, widget)
    page.clock.run_for(1)
    state(widget, "accepted")
    pending = []
    page.route("**/submissions/saved", lambda route: pending.append(route))
    with page.expect_request("**/submissions/saved"):
        page.clock.run_for(2000)
    press(page, widget)
    state(widget, "recording")
    pending[0].fulfill(json={"state": "result_ready", "transcript": "Старый результат"})
    expect(widget.get_by_role("status")).to_have_text("Идёт запись.")
    expect(widget.locator(".current")).to_be_empty()
    page.keyboard.press("Escape")
    assert len(uploads) == 1
