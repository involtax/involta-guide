import os
from pathlib import Path

import pytest
from integration.test_voice_upload_transport import serving
from playwright.sync_api import sync_playwright
from starlette.responses import HTMLResponse, Response

from involta_guide.main import create_app


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_warm_browser_cache_does_not_mix_old_panorama_assets_with_new_html(database, engine):
    executable = (
        os.getenv("WEBKIT_EXECUTABLE") if engine == "webkit" else os.getenv("CHROMIUM_PATH")
    )
    if engine == "webkit" and not executable:
        pytest.skip("Set WEBKIT_EXECUTABLE to run the additional WebKit cache regression")
    settings, _ = database
    settings = settings.model_copy(update={"release_sha": "b" * 40})
    app = create_app(settings)
    asset_root = Path(__file__).parents[2] / "src/involta_guide/web/static"
    requests = []

    async def upgrade_server(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        path, query = scope["path"], scope["query_string"].decode()
        requests.append(path + ("?" + query if query else ""))
        if path == "/warm-cache":
            response = HTMLResponse(
                '<link rel="stylesheet" href="/guide/assets/panorama.css">'
                '<script src="/guide/assets/panorama.js"></script>'
            )
        elif not query and path in ("/guide/assets/panorama.css", "/guide/assets/panorama.js"):
            # The previous release used these same URLs. Keep its layout/script
            # cached while serving the actual new application on the next page.
            css = path.endswith(".css")
            body = (
                (asset_root / "panorama.css").read_text() + "\n.panorama-track {display:block}"
                if css
                else "window.oldPanoramaScript = true;"
            )
            response = Response(
                body,
                media_type="text/css" if css else "text/javascript",
                headers={"Cache-Control": "public, max-age=3600"},
            )
        else:
            return await app(scope, receive, send)
        await response(scope, receive, send)

    with serving(upgrade_server) as origin, sync_playwright() as p:
        browser = getattr(p, engine).launch(
            executable_path=executable or None,
            args=["--no-sandbox"] if engine == "chromium" else [],
        )
        page = browser.new_page(viewport={"width": 390, "height": 950}, has_touch=True)
        page.goto(origin + "/warm-cache", wait_until="networkidle")
        assert page.evaluate("window.oldPanoramaScript")
        page.goto(origin + "/guide/ivanovo", wait_until="networkidle")
        image = page.locator(".panorama-image")
        secret = page.locator(".panorama-secret")
        image.evaluate("el=>el.decode()")
        page.wait_for_timeout(150)
        city_box, secret_box = image.bounding_box(), secret.bounding_box()
        assert secret_box["x"] >= city_box["x"] + city_box["width"] - 1
        assert secret_box["y"] < city_box["y"] + city_box["height"]
        assert page.locator(".panorama").get_attribute("data-panorama-enhanced") == ""
        assert requests.count("/guide/assets/panorama.css") == 1
        assert requests.count("/guide/assets/panorama.js") == 1
        assert settings.asset("/assets/panorama.css") in requests
        assert settings.asset("/assets/panorama.js") in requests
        scroller = page.locator(".panorama-scroller")
        scroller.focus()
        scroller.press("End")
        assert scroller.evaluate("el=>el.scrollLeft") > 610
        page.wait_for_timeout(2400)
        assert scroller.evaluate("el=>el.scrollLeft") <= 610
        browser.close()
