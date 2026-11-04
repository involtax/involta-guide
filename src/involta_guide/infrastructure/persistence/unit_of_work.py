from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from involta_guide.config import Settings

from .repositories.postgres import PostgresRepository


def create_database(settings: Settings) -> Engine:
    url = settings.database_url.get_secret_value().replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=8,
        max_overflow=8,
        connect_args={
            "connect_timeout": 10,
            "options": f"-c search_path={settings.database_schema} -c statement_timeout=30000",
        },
        hide_parameters=True,
    )


class PostgresUow:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def __enter__(self) -> "PostgresUow":
        self.session = Session(self.engine)
        self.session.begin()
        self.repo = PostgresRepository(self.session)
        return self

    def __exit__(self, kind: Any, value: Any, traceback: Any) -> None:
        try:
            if kind is None:
                self.session.commit()
            else:
                self.session.rollback()
        finally:
            self.session.close()
