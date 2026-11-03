"""Local real-microphone stand. The browser mocks upload; no Guide/Voice API is contacted."""

import argparse
import mimetypes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/involta_voice/static"


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path in ("/", "/guide/ivanovo"):
            source = ROOT / "tools/voice-widget-demo.html"
        elif path == "/guide/assets/style.css":
            source = ROOT / "src/involta_guide/web/static/style.css"
        elif path.startswith("/guide/voice/"):
            source = (STATIC / path.removeprefix("/guide/voice/")).resolve()
            if not source.is_relative_to(STATIC):
                self.send_error(404)
                return
        else:
            self.send_error(404)
            return
        if not source.is_file():
            self.send_error(404)
            return
        body = source.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type", mimetypes.guess_type(source)[0] or "application/octet-stream"
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # The stand does not log cookies, audio or audio-derived data.


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=33090)
    args = parser.parse_args()
    print(f"Voice widget stand: http://localhost:{args.port}/guide/ivanovo", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
