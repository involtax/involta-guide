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


@pytest.fixture
def database(tmp_path, monkeypatch, request):
    values = dotenv_values(ROOT / ".env.local")
    url = os.getenv("TEST_DATABASE_URL") or values.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is required for PostgreSQL tests")
    schema = "test_py_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATABASE_SCHEMA", schema)
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-" + "x" * 40)
    settings = Settings(
        database_url=SecretStr(url),
        database_schema=schema,
        session_secret=SecretStr("test-session-secret-" + "x" * 40),
        admin_password=SecretStr("test-password-123456"),
        alex_secret=SecretStr("alex-test-" + "x" * 40),
        openai_api_key=SecretStr(""),
        site_url="http://testserver/guide",
        data_dir=tmp_path,
        root_dir=ROOT,
    )
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    engine = create_database(settings)
    try:
        seed(PostgresUow(engine), ROOT)
        yield settings, engine
    finally:
        engine.dispose()
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
