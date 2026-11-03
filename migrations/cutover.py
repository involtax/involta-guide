"""One-time import of current content. Old log history is intentionally not migrated."""

import re
from pathlib import Path

from sqlalchemy import Connection, inspect, text

BASELINE = "current_state"
SUPPORTED = {"0007_voice_package", "0006_coverage", "0006_voice", "0005_current"}


def adopt_current_state(connection: Connection) -> None:
    tables = set(inspect(connection).get_table_names())
    if "locations" not in tables or "versions" not in tables:
        return
    revisions = (
        set(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
        if "alembic_version" in tables
        else set()
    )
    if not revisions or not revisions <= SUPPORTED:
        raise RuntimeError(
            "Unsupported existing schema. Expected the single-current-page Guide release; no changes made."
        )
    # All DDL/data changes and stamping run under the same transaction/advisory lock.
    connection.exec_driver_sql(Path(__file__).with_name("cutover.sql").read_text())
    if "review_decisions" in tables:
        connection.execute(
            text("""UPDATE review_decisions
            SET before_data=before_data-ARRAY['content','affected'],
                after_data=jsonb_set(after_data,'{result}',coalesce(after_data->'result','{}'::jsonb)-ARRAY['content_after','versions']),
                result=result-ARRAY['content_after','versions']""")
        )
    # Install the Voice boundary if the previous release predates it. No old migration replay.
    baseline = Path(__file__).with_name("current_schema.sql").read_text()
    created = set()
    for statement in re.findall(r"CREATE TABLE .*?;", baseline, re.S):
        name = statement.split()[2]
        if name not in tables:
            connection.exec_driver_sql(statement)
            created.add(name)
    for statement in re.findall(r"CREATE (?:UNIQUE )?INDEX .*?;", baseline, re.S):
        match = re.search(r" ON (\w+) ", statement)
        if match and match[1] in created:
            connection.exec_driver_sql(statement)
    connection.execute(
        text("""INSERT INTO review_texts(review_id,original_text,editorial_text,public)
        SELECT id,transcript,transcript,(moderation='approved' AND fraud=0) FROM reviews
        WHERE transcript IS NOT NULL AND transcript<>'' AND alex_session_id IS NULL AND admin_session_id IS NULL
        ON CONFLICT(review_id) DO NOTHING""")
    )
    connection.execute(
        text("INSERT INTO voice_cursor(consumer) VALUES('voice') ON CONFLICT DO NOTHING")
    )
    connection.execute(text("DELETE FROM alembic_version"))
    connection.execute(
        text("INSERT INTO alembic_version(version_num) VALUES(:revision)"), {"revision": BASELINE}
    )
