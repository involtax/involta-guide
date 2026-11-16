import os
from pathlib import Path

import pytest
from integration.test_editorial import CARD, services
from integration.test_voice_upload_transport import serving
from playwright.sync_api import expect, sync_playwright

from involta_guide.main import create_app
from involta_guide.web.panoramas import PANORAMA_DIRECTORY


def prepared_panorama(database):
    settings, _ = database
    editor, queries, _ = services(database)
    location = queries.public("ples")["location"]
    editor.save(
        location["id"],
        {
            "intro": "Два ресторана с русской кухней и разным характером на берегу Волги.",
            "restaurants": [
                {**CARD, "name": "Грандъ Сова", "description": "Камерный ресторан в Плёсе."},
                {**CARD, "name": "Усадьба Плёс", "mapsUrl": "https://yandex.ru/maps/org/456/"},
            ],
        },
        None,
    )
    return create_app(settings)


@pytest.mark.parametrize("width", [320, 390, 650, 651, 1440])
def test_panorama_geometry_motion_and_full_manual_range(database, tmp_path, width):
    with serving(prepared_panorama(database)) as origin, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page(viewport={"width": width, "height": 950}, has_touch=width <= 650)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin + "/guide/ples", wait_until="networkidle")
        if width == 320:
            for asset in PANORAMA_DIRECTORY.glob("*.avif"):
                response = page.request.get(origin + "/guide/assets/panoramas/" + asset.name)
                assert response.status == 200
                assert response.headers["content-type"].split(";")[0] == "image/avif"
                decoded = page.evaluate(
                    """async url => {
                    const img = new Image(); img.src = url; await img.decode();
                    const canvas = document.createElement('canvas');
                    canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
                    const ctx = canvas.getContext('2d'); ctx.drawImage(img, 0, 0);
                    const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
                    let transparent = false, visible = false;
                    for (let i = 3; i < pixels.length; i += 4) {
                        transparent ||= pixels[i] < 255; visible ||= pixels[i] > 0;
                    }
                    return [img.naturalWidth, img.naturalHeight, transparent, visible];
                }""",
                    origin + "/guide/assets/panoramas/" + asset.name,
                )
                assert decoded == [2172, 724, True, True], asset.name
        scroller = page.locator(".panorama-scroller")
        image = scroller.locator(".panorama-image")
        track = scroller.locator(".panorama-track")
        expect(image).to_be_visible()
        geometry = page.locator(".panorama").evaluate("""el=>{
            const image=el.querySelector('img'), box=el.getBoundingClientRect();
            return {x:box.x,width:box.width,imageWidth:image.width,imageHeight:image.height,
                naturalWidth:image.naturalWidth,naturalHeight:image.naturalHeight,
                marginTop:parseFloat(getComputedStyle(el).marginTop)};
        }""")
        assert geometry["naturalWidth"] == 2172 and geometry["naturalHeight"] == 724
        assert abs(geometry["x"]) < 1 and abs(geometry["width"] - width) < 1
        expected_width = 1000 if width <= 650 else width
        assert geometry["imageWidth"] == expected_width
        assert abs(geometry["imageHeight"] - expected_width / 3) <= 1
        assert abs(geometry["marginTop"] + max(120, min(width * 0.2, 280))) < 1
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        expect(page.get_by_role("heading", name="Стоит зайти", exact=True)).to_have_count(0)
        expect(page.locator(".restaurant").first).to_have_css("border-top-width", "0px")
        expect(page.locator(".restaurant").nth(1)).to_have_css("border-top-width", "1px")
        expect(scroller).to_have_css("scrollbar-width", "none")
        folder = Path(os.getenv("PANORAMA_SCREENSHOTS_DIR", str(tmp_path)))
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / f"panorama-{width}.png"), full_page=True)
        expect(page.locator(".panorama button")).to_have_count(0)
        if width <= 650:
            position = scroller.evaluate("el=>el.scrollLeft")
            assert abs(position - (1000 - width - 120)) < 20
            scroller.focus()  # Focus does not pause motion.
            page.wait_for_function(
                "position=>document.querySelector('.panorama-scroller').scrollLeft<position",
                arg=position,
            )
            before = scroller.evaluate("el=>el.scrollLeft")
            box = scroller.bounding_box()
            page.mouse.move(width / 2, box["y"] + box["height"] - 20)
            page.mouse.wheel(0, 90)  # Vertical wheel scrolls the page, motion continues.
            page.wait_for_timeout(400)
            assert page.evaluate("scrollY") > 0
            assert scroller.evaluate("el=>el.scrollLeft") < before
            scroller.focus()
            scroller.press("End")
            assert scroller.evaluate("el=>el.scrollLeft") == scroller.evaluate(
                "el=>el.scrollWidth-el.clientWidth"
            )
            box = scroller.bounding_box()
            page.mouse.move(width / 2, box["y"] + box["height"] - 30)
            page.mouse.wheel(180, 0)
            page.wait_for_timeout(35)
            assert track.evaluate("el=>getComputedStyle(el).transform") != "none"
            page.wait_for_timeout(650)
            assert track.evaluate("el=>getComputedStyle(el).transform") == "none"
            scroller.press("Home")
            assert scroller.evaluate("el=>el.scrollLeft") == 0
            # Dragging has the full range too, beyond the inset used by autoplay.
            box = scroller.bounding_box()
            page.mouse.move(width - 70, box["y"] + box["height"] - 30)
            page.mouse.down()
            page.mouse.move(20, box["y"] + box["height"] - 30, steps=8)
            page.mouse.up()
            assert scroller.evaluate("el=>el.scrollLeft") > 100
        else:
            assert scroller.evaluate("el=>el.scrollLeft") == 0
            page.wait_for_timeout(350)
            assert scroller.evaluate("el=>el.scrollLeft") == 0
        scroller.focus()
        scroller.press("End")
        end = scroller.evaluate("el=>el.scrollWidth-el.clientWidth")
        assert scroller.evaluate("el=>el.scrollLeft") == end
        secret = scroller.locator(".panorama-secret")
        secret.evaluate("el=>el.decode()")
        assert secret.evaluate("el=>[el.naturalWidth,el.naturalHeight]") == [6516, 724]
        page.wait_for_timeout(400)
        assert scroller.evaluate("el=>el.scrollLeft") == end
        page.screenshot(path=str(folder / f"secret-{width}.png"))
        assert scroller.evaluate("el=>el.scrollLeft") == end
        page.wait_for_timeout(2100)
        assert scroller.evaluate("el=>el.scrollLeft") <= expected_width - width
        page.goto(origin + "/guide/kineshma", wait_until="networkidle")
        expect(page.locator("[data-panorama]")).to_have_count(0)
        assert "/assets/panorama.js" not in page.content()
        assert "/assets/panorama.css" not in page.content()
        assert not errors, errors
        browser.close()


def test_reduced_motion_and_no_javascript_keep_the_panorama_usable(database):
    with serving(prepared_panorama(database)) as origin, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        for javascript in (True, False):
            page = browser.new_page(
                viewport={"width": 390, "height": 950},
                reduced_motion="reduce",
                java_script_enabled=javascript,
            )
            page.goto(origin + "/guide/ples", wait_until="networkidle")
            scroller = page.locator(".panorama-scroller")
            expect(scroller.locator(".panorama-image")).to_be_visible()
            expect(page.locator(".panorama button")).to_have_count(0)
            position = scroller.evaluate("el=>el.scrollLeft")
            page.wait_for_timeout(350)
            assert scroller.evaluate("el=>el.scrollLeft") == position
            scroller.focus()
            if javascript:
                scroller.press("End")
            else:
                box = scroller.bounding_box()
                page.mouse.move(195, box["y"] + box["height"] - 30)
                page.mouse.wheel(4000, 0)
            page.wait_for_timeout(250)  # Native scrolling without JS is asynchronous.
            assert scroller.evaluate("el=>el.scrollLeft") == 1780
            if javascript:
                page.emulate_media(reduced_motion="no-preference")
                page.wait_for_timeout(2100)
                assert scroller.evaluate("el=>el.scrollLeft") <= 610
                scroller.press("Home")
                page.wait_for_function(
                    "document.querySelector('.panorama-scroller').scrollLeft > 0"
                )
            page.close()
        browser.close()


def test_touch_scrolls_the_picture_horizontally_and_the_page_vertically(database):
    with serving(prepared_panorama(database)) as origin, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page(viewport={"width": 390, "height": 950}, has_touch=True)
        page.goto(origin + "/guide/ples", wait_until="networkidle")
        scroller = page.locator(".panorama-scroller")
        session = page.context.new_cdp_session(page)

        def swipe(dx, dy):
            box = scroller.bounding_box()
            x, y = 270, box["y"] + box["height"] * 0.7
            session.send(
                "Input.dispatchTouchEvent",
                {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]},
            )
            for step in range(1, 9):
                session.send(
                    "Input.dispatchTouchEvent",
                    {
                        "type": "touchMove",
                        "touchPoints": [{"x": x + dx * step / 8, "y": y + dy * step / 8}],
                    },
                )
                page.wait_for_timeout(25)
            session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

        before = scroller.evaluate("el=>el.scrollLeft")
        swipe(0, -104)
        page.wait_for_timeout(600)
        assert page.evaluate("scrollY") > 25
        assert abs(scroller.evaluate("el=>el.scrollLeft") - before) < 10
        page.evaluate("scrollTo(0,0)")
        page.evaluate("""() => {
            window.touchCancels = 0;
            document.querySelector('.panorama-scroller').addEventListener(
                'pointercancel', () => window.touchCancels++);
        }""")
        # Native scrolling cancels Pointer Events but must finish the whole swipe.
        for dx, dy in [(-180, 0), (-180, 20), (-180, -20)]:
            scroller.evaluate("el=>el.scrollLeft=250")
            swipe(dx, dy)
            page.wait_for_timeout(250)
            assert scroller.evaluate("el=>el.scrollLeft") > 390
        assert page.evaluate("window.touchCancels") >= 3
        assert page.evaluate("scrollY") < 1
        scroller.evaluate("el=>el.scrollLeft=610")
        swipe(-180, 0)
        page.wait_for_timeout(700)
        secret_position = scroller.evaluate("el=>el.scrollLeft")
        assert secret_position > 700
        # Holding the secret pauses its return even after the browser cancelled
        # Pointer Events for the preceding native swipe.
        box = scroller.bounding_box()
        session.send(
            "Input.dispatchTouchEvent",
            {
                "type": "touchStart",
                "touchPoints": [{"x": 180, "y": box["y"] + box["height"] * 0.7}],
            },
        )
        held = scroller.evaluate("el=>el.scrollLeft")
        page.wait_for_timeout(1400)
        assert abs(scroller.evaluate("el=>el.scrollLeft") - held) < 2
        session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_timeout(3500)
        assert scroller.evaluate("el=>el.scrollLeft") <= 610
        browser.close()


def test_autoplay_stays_in_city_after_full_round_trip(database):
    with serving(prepared_panorama(database)) as origin, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page(viewport={"width": 390, "height": 950})
        page.clock.install()
        page.goto(origin + "/guide/ples", wait_until="networkidle")
        page.evaluate("""() => {
            window.panoramaPositions = [];
            const scroller = document.querySelector('.panorama-scroller');
            setInterval(() => panoramaPositions.push(scroller.scrollLeft), 500);
        }""")
        page.clock.run_for(90000)
        positions = page.evaluate("panoramaPositions")
        assert min(positions) >= 119
        assert max(positions) <= 491
        assert max(positions) - min(positions) > 300
        assert positions[-1] > min(positions) + 100
        browser.close()


def test_secret_return_waits_for_release_and_can_be_interrupted(database):
    with serving(prepared_panorama(database)) as origin, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page(viewport={"width": 390, "height": 950})
        page.goto(origin + "/guide/ples", wait_until="networkidle")
        scroller = page.locator(".panorama-scroller")
        scroller.focus()
        scroller.press("End")
        box = scroller.bounding_box()
        page.mouse.move(180, box["y"] + box["height"] - 40)
        page.mouse.down()
        end = scroller.evaluate("el=>el.scrollLeft")
        page.wait_for_timeout(1400)
        assert scroller.evaluate("el=>el.scrollLeft") == end
        page.mouse.up()
        page.wait_for_timeout(750)
        assert scroller.evaluate("el=>el.scrollLeft") == end
        page.wait_for_timeout(500)
        moving = scroller.evaluate("el=>el.scrollLeft")
        assert 610 < moving < end
        page.mouse.down()
        stopped = scroller.evaluate("el=>el.scrollLeft")
        page.wait_for_timeout(1200)
        assert scroller.evaluate("el=>el.scrollLeft") == stopped
        page.mouse.up()
        # Wheel activity restarts the one-second countdown, even at the edge.
        scroller.press("End")
        for _ in range(3):
            page.wait_for_timeout(600)
            page.mouse.wheel(-10, 0)
        current = scroller.evaluate("el=>el.scrollLeft")
        assert current > end - 50
        page.wait_for_timeout(2300)
        assert scroller.evaluate("el=>el.scrollLeft") <= 610
        browser.close()
