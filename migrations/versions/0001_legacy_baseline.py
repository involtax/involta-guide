"""Adopt the audited legacy migration ledger without replaying applied DDL."""

import hashlib
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "0001_legacy"
down_revision = None


def upgrade() -> None:
    connection = op.get_bind()
    tracked = connection.execute(text("SELECT to_regclass('schema_migrations')")).scalar()
    existing = connection.execute(
        text("SELECT to_regclass('locations'),to_regclass('cities')")
    ).one()
    if not tracked and any(existing):
        raise RuntimeError(
            "Existing untracked database: audit and adopt legacy ledger explicitly first"
        )
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations(name text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())"
        )
    )
    for path in sorted(Path(__file__).parents[1].glob("*.sql")):
        sql = path.read_text()
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        previous = connection.execute(
            text("SELECT checksum FROM schema_migrations WHERE name=:name"), {"name": path.name}
        ).scalar()
        if previous:
            if previous != checksum:
                raise RuntimeError(f"Legacy checksum mismatch: {path.name}")
            continue
        connection.exec_driver_sql(sql)
        connection.execute(
            text("INSERT INTO schema_migrations(name,checksum) VALUES(:name,:checksum)"),
            {"name": path.name, "checksum": checksum},
        )


def downgrade() -> None:
    raise RuntimeError(
        "Destructive downgrade disabled; roll back application image or restore authorized backup"
    )
