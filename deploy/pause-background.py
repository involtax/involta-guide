"""Run inside the rollback image before starting its worker. Never send API requests."""

import importlib.util

from sqlalchemy import Engine, text


def pause_background(engine: Engine) -> None:
    with engine.begin() as conn:
        if conn.execute(text("SELECT to_regclass('generation_requests')")).scalar_one() is None:
            return
        conn.execute(
            text("""UPDATE generation_requests g SET
            state=CASE WHEN response_id IS NULL THEN 'unknown'
                       WHEN state='completed' THEN state ELSE 'paused' END,
            updated_at=now()
            FROM jobs j WHERE g.job_id=j.id AND j.status IN ('queued','running')""")
        )
        conn.execute(
            text("""UPDATE jobs j SET status='failed',finished_at=now(),
            lease_owner=NULL,lease_until=NULL,
            error='Фоновый запрос сохранён и приостановлен при откате на старый обработчик.'
            WHERE status IN ('queued','running') AND EXISTS(
                SELECT 1 FROM generation_requests g WHERE g.job_id=j.id)""")
        )


if __name__ == "__main__":
    if importlib.util.find_spec("involta_guide.infrastructure.ai.background") is None:
        from involta_guide.config import Settings
        from involta_guide.infrastructure.persistence.unit_of_work import create_database

        database = create_database(Settings())
        try:
            pause_background(database)
            print("Background requests preserved and paused for the older worker")
        finally:
            database.dispose()
