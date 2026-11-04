import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from involta_voice.config import Settings as VoiceSettings
from involta_voice.contracts import LocationContext
from involta_voice.db import database as voice_engine
from involta_voice.db import migrate
from involta_voice.provider import FakeProvider
from involta_voice.service import VoiceService
from involta_voice.worker import Worker

from .test_voice_service import metadata, recording


@pytest.fixture
def shared_voice(database, tmp_path):
    guide, engine = database
    settings = VoiceSettings(
        database_url=guide.database_url,
        database_schema="voice_runtime_" + uuid4().hex,
        service_secret="local-runtime-secret-" + "x" * 40,
        data_dir=tmp_path / "voice",
        min_free_bytes=0,
        fake_provider=True,
        asr_beta_enabled=False,
    )
    storage = voice_engine(settings)
    migrate(storage)
    yield guide, engine, settings, storage
    storage.dispose()
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA " + settings.database_schema + " CASCADE")


@contextmanager
def server(shared_voice, tmp_path, module):
    guide, _, settings, _ = shared_voice
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "DATABASE_URL": guide.database_url.get_secret_value(),
        "DATABASE_SCHEMA": guide.database_schema,
        "VOICE_DATABASE_SCHEMA": settings.database_schema,
        "SESSION_SECRET": guide.session_secret.get_secret_value(),
        "VOICE_SERVICE_SECRET": settings.service_secret.get_secret_value(),
        "VOICE_URL": base,
        "VOICE_CONSUMER_STATE": str(tmp_path / "consumer.json"),
        "VOICE_DATA_DIR": str(settings.data_dir),
        "DATA_DIR": str(tmp_path / "guide"),
        "SITE_URL": base + "/guide",
        "OPENAI_API_KEY": "",
        "COOKIE_SECURE": "false",
        "VOICE_FAKE_PROVIDER": "true",
        "VOICE_ASR_BETA_ENABLED": "false",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    with (tmp_path / "server.log").open("w+") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                module + ":create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--timeout-graceful-shutdown",
                "1",
                "--no-access-log",
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            path = "/guide/health/ready" if module == "involta_guide.main" else "/health/ready"
            with httpx.Client(trust_env=False, timeout=2) as client:
                for _ in range(80):
                    try:
                        if client.get(base + path).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if process.poll() is not None:
                        log.seek(0)
                        pytest.fail(log.read())
                    time.sleep(0.1)
                else:
                    pytest.fail("Server did not become ready")
            yield process, base, port
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def test_combined_voice_delivers_once_and_stops_without_waiting_for_backoff(shared_voice, tmp_path):
    guide, engine, settings, storage = shared_voice
    with engine.connect() as conn:
        location = (
            conn.execute(text("SELECT id,slug,name FROM locations WHERE slug='ivanovo'"))
            .mappings()
            .one()
        )
    meta = metadata().model_copy(
        update={
            "location": LocationContext(
                id=UUID(str(location["id"])),
                slug=location["slug"],
                name=location["name"],
                content_epoch=1,
            )
        }
    )
    service = VoiceService(storage, settings)
    reference = service.submit(meta, recording(), "audio/wav")["reference"]
    worker = Worker(storage, settings, FakeProvider())
    assert worker.once() and worker.once()
    for _ in range(2):
        with server(shared_voice, tmp_path, "involta_guide.infrastructure.voice.runtime") as (
            process,
            _,
            _,
        ):
            for attempt in range(100):
                with engine.connect() as conn:
                    count = conn.execute(
                        text("SELECT count(*) FROM reviews WHERE id=:id"), {"id": reference}
                    ).scalar_one()
                    delivered = conn.execute(
                        text("SELECT count(*) FROM voice_inbox WHERE state='imported'")
                    ).scalar_one()
                if count == 1 and delivered == 2:
                    break
                time.sleep(0.1)
            else:
                pytest.fail("Combined service did not deliver both events")
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=6)
            assert time.monotonic() - started < 5
            assert process.returncode in (0, -signal.SIGTERM)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM reviews")).scalar_one() == 1


@pytest.mark.parametrize(
    "module,path",
    [
        ("involta_guide.main", "/guide/admin/login"),
        ("involta_guide.infrastructure.voice.runtime", "/internal/v1/submissions"),
    ],
)
def test_incomplete_http_body_does_not_hold_shutdown_forever(shared_voice, tmp_path, module, path):
    with server(shared_voice, tmp_path, module) as (process, base, port):
        with socket.create_connection(("127.0.0.1", port)) as connection:
            secret = shared_voice[2].service_secret.get_secret_value()
            connection.sendall(
                (
                    f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    f"Origin: {base}\r\nAuthorization: Bearer {secret}\r\n"
                    "Content-Type: application/x-www-form-urlencoded\r\n"
                    "Content-Length: 100000\r\n\r\nmetadata="
                ).encode()
            )
            time.sleep(0.2)
            started = time.monotonic()
            process.terminate()
            process.wait(timeout=6)
            assert time.monotonic() - started < 5
            assert process.returncode in (0, -signal.SIGTERM)
