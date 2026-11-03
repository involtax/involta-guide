import importlib.util
from pathlib import Path

from alembic import context
from sqlalchemy import text

from involta_guide.config import Settings
from involta_guide.infrastructure.persistence.unit_of_work import create_database


def run() -> None:
    settings = Settings()
    engine = create_database(settings)
    with engine.connect() as connection:
        context.configure(connection=connection, version_table_schema=settings.database_schema)
        with context.begin_transaction():
            connection.execute(text("SELECT pg_advisory_xact_lock(47031001)"))
            spec = importlib.util.spec_from_file_location(
                "guide_cutover", Path(__file__).with_name("cutover.py")
            )
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.adopt_current_state(connection)
            context.run_migrations()
    engine.dispose()


run()
