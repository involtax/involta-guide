import hashlib
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import uuid4


def test_download_verifies_original_hash_reuses_audio_and_prunes_only_deleted_reviews(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "voice_download", Path(__file__).resolve().parents[2] / "tools/download-voice-corpus.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ids = [str(uuid4()), str(uuid4())]
    audio = b"original recording bytes"
    state = {"ids": ids.copy(), "audio_requests": 0, "corrupt": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/guide/logs/reviews.json"):
                content = {
                    "reviews": [
                        {
                            "id": identity,
                            "number": i + 1,
                            "slug": "ivanovo",
                            "json_url": f"/guide/{identity}.json",
                            "audio_url": f"/guide/{identity}.webm",
                        }
                        for i, identity in enumerate(state["ids"])
                    ],
                    "next_after": None,
                }
                data = json.dumps(content).encode()
            elif self.path.endswith(".json"):
                data = json.dumps(
                    {"voice": {"mime": "audio/webm", "sha256": hashlib.sha256(audio).hexdigest()}}
                ).encode()
            else:
                state["audio_requests"] += 1
                data = b"corrupt" if state["corrupt"] else audio
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = tmp_path / "corpus"
    base = f"http://127.0.0.1:{server.server_port}/guide/"
    try:
        result = module.download(base, output)
        assert len(result["reviews"]) == 2 and not result["errors"] and state["audio_requests"] == 2
        assert (output / (ids[0] + ".webm")).read_bytes() == audio
        assert (output / (ids[0] + ".webm")).stat().st_mode & 0o777 == 0o600
        unrelated = output / "notes.txt"
        unrelated.write_text("keep")
        state["ids"] = [ids[1]]
        result = module.download(base, output)
        assert state["audio_requests"] == 2
        assert (
            not (output / (ids[0] + ".json")).exists()
            and not (output / (ids[0] + ".webm")).exists()
        )
        assert unrelated.read_text() == "keep" and len(result["reviews"]) == 1
        (output / (ids[1] + ".webm")).unlink()
        state["corrupt"] = True
        result = module.download(base, output)
        assert len(result["errors"]) == 1 and "SHA256 mismatch" in result["errors"][0]["error"]
        assert result["reviews"] == []
        assert not (output / (ids[1] + ".webm")).exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
