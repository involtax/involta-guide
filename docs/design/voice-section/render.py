"""Render the four static proposals in Chromium; no application backend, mic or API requests."""

import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent


def main():
    handler = partial(SimpleHTTPRequestHandler, directory=str(ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        render(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def render(base):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH")
            or ("/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None),
            args=["--no-sandbox"],
        )
        page = browser.new_page(viewport={"width": 1238, "height": 650}, device_scale_factor=1)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        for number in range(1, 6):
            page.goto(base + f"/preview.html?variant={number}", wait_until="load")
            for frame in page.frames[1:]:
                frame.wait_for_selector("button svg")
                assert frame.evaluate("document.documentElement.scrollWidth <= innerWidth")
                button = frame.locator("button").bounding_box()
                assert button and button["width"] >= 44 and button["height"] >= 44
                assert frame.locator("h2").is_visible()
            name = "02-approved.png" if number == 5 else f"{number:02d}.png"
            page.screenshot(path=ROOT / "screenshots" / name, full_page=True)
        assert not errors, errors
        browser.close()
        print(
            "Rendered 4 alternatives and the selected revision: desktop + mobile, no horizontal overflow or JS errors."
        )


if __name__ == "__main__":
    main()
