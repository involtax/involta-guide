import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from psycopg import sql
from pydantic import SecretStr

from involta_guide.config import Settings
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow, create_database
from involta_guide.infrastructure.seed import seed

ROOT = Path(__file__).resolve().parents[1]


def pytest_configure(config):
    config.addinivalue_line("markers", "full_catalog: use all versioned locations, not sample data")


@pytest.fixture(scope="session")
def sample_catalog_root(tmp_path_factory):
    """Small real catalogue for ordinary behaviour tests; production data is untouched."""
    from involta_guide.infrastructure.location_catalog import load_catalog

    catalog = load_catalog(ROOT)
    by_slug = {row["slug"]: row for row in catalog["locations"]}
    selected = {
        "abrau-dyurso",
        "anapa",
        "gazprom-polyana",
        "krasnaya-polyana-kurort",
        "pereslavl-zalessky",
        "ivanovo",
        "kazan",
        "kineshma",
        "kostroma",
        "medvezhegorsk",
        "moskva",
        "ples",
        "shuya",
        "sirius",
        "sochi",
        "veliky-novgorod",
        "yaroslavl",
    }
    for slug in tuple(selected):
        parent = by_slug[slug]["parent_slug"]
        while parent:
            selected.add(parent)
            parent = by_slug[parent]["parent_slug"]
    ids = {by_slug[slug]["id"] for slug in selected}
    sample = {
        **catalog,
        "locations": [
            {**row, "nearby_location_ids": [n for n in row["nearby_location_ids"] if n in ids]}
            for row in catalog["locations"]
            if row["slug"] in selected
        ],
        "redirects": [row for row in catalog["redirects"] if row["location_slug"] in selected],
    }
    root = tmp_path_factory.mktemp("sample-catalog")
    (root / "data/prompts").mkdir(parents=True)
    (root / "data/location-catalog.json").write_text(json.dumps(sample, ensure_ascii=False))
    prompt = Path("data/prompts/restaurant-research-v4.txt")
    (root / prompt).write_text((ROOT / prompt).read_text())
    return root


@pytest.fixture
def database(tmp_path, monkeypatch, request, sample_catalog_root):
    values = dotenv_values(ROOT / ".env.local")
    url = os.getenv("TEST_DATABASE_URL") or values.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is required for PostgreSQL tests")
    schema = "test_py_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATABASE_SCHEMA", schema)
    monkeypatch.setenv("VOICE_DATABASE_SCHEMA", "voice_" + schema)
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-" + "x" * 40)
    settings = Settings(
        database_url=SecretStr(url),
        database_schema=schema,
        session_secret=SecretStr("test-session-secret-" + "x" * 40),
        admin_password=SecretStr("test-password-123456"),
        openai_api_key=SecretStr(""),
        site_url="http://testserver/guide",
        data_dir=tmp_path,
        root_dir=ROOT,
    )
    config = Config(str(ROOT / "alembic.ini"))
    engine = create_database(settings)
    try:
        command.upgrade(config, "head")
        catalog_root = (
            ROOT if request.node.get_closest_marker("full_catalog") else sample_catalog_root
        )
        seed(PostgresUow(engine), catalog_root)
        yield settings, engine
    finally:
        engine.dispose()
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.fixture
def streaming_webm(tmp_path):
    import subprocess

    data = subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "6.2",
            "-c:a",
            "libopus",
            "-live",
            "1",
            "-fflags",
            "+bitexact",
            "-f",
            "webm",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    ).stdout
    path = tmp_path / "stream.webm"
    path.write_bytes(data)
    return path
